import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tiktok_daily.common import WorkflowError, config, due_time
from tiktok_daily.publishing import Buffer, GitHub, post_summary, public_media_check
from tiktok_daily.research import is_fresh, parse_trends
from tiktok_daily.video import subtitles, timestamp
from tiktok_daily.writer import validate_package, finalize_package
from tiktok_daily.__main__ import live


class ClockTests(unittest.TestCase):
    def test_lusaka_0800_is_0600_utc(self):
        now=datetime(2026,10,20,7,20,tzinfo=ZoneInfo("Africa/Lusaka"))
        self.assertEqual(due_time("2026-10-20",config(),now),"2026-10-20T06:00:00Z")

    def test_delayed_runner_stays_on_same_local_day(self):
        now=datetime(2026,10,20,9,0,tzinfo=ZoneInfo("Africa/Lusaka"))
        self.assertEqual(due_time("2026-10-20",config(),now),"2026-10-20T07:03:00Z")

    def test_no_rollover_or_historical_publishing(self):
        now=datetime(2026,10,20,23,59,tzinfo=ZoneInfo("Africa/Lusaka"))
        with self.assertRaises(WorkflowError):
            due_time("2026-10-20",config(),now)
        with self.assertRaises(WorkflowError):
            due_time("2026-10-19",config(),now)

    def test_stale_future_and_missing_dates_fail_closed(self):
        now=datetime(2026,10,20,6,tzinfo=timezone.utc)
        self.assertFalse(is_fresh("Fri, 16 Oct 2026 06:00:00 GMT",now,36))
        self.assertFalse(is_fresh("Wed, 21 Oct 2026 06:00:00 GMT",now,36))
        self.assertFalse(is_fresh(None,now,36))
        self.assertTrue(is_fresh("Tue, 20 Oct 2026 05:00:00 GMT",now,36))


class BufferTests(unittest.TestCase):
    def test_current_graphql_video_contract_and_ai_label(self):
        api=Buffer("test-secret")
        seen={}
        def fake(query,variables):
            seen.update(variables["input"])
            self.assertIn("CreatePostInput!",query)
            return {"createPost":{"__typename":"PostActionSuccess","post":{"id":"p1","status":"scheduled","schedulingType":"automatic"}}}
        with patch.object(api,"query",side_effect=fake):
            post=api.create("t1","Original caption","https://example.com/video.mp4","2026-10-20T06:00:00Z")
        self.assertEqual(post["id"],"p1")
        self.assertEqual(seen["assets"],[{"video":{"url":"https://example.com/video.mp4","metadata":{"thumbnailOffset":2000}}}])
        self.assertTrue(seen["metadata"]["tiktok"]["isAiGenerated"])
        self.assertFalse(seen["needsApproval"])
        self.assertEqual(seen["mode"],"customScheduled")

    def test_mutation_error_never_reports_success(self):
        api=Buffer("test-secret")
        with patch.object(api,"query",return_value={"createPost":{"__typename":"LimitReachedError","message":"Queue is full"}}):
            with self.assertRaisesRegex(WorkflowError,"Queue is full"):
                api.create("t1","caption","https://example.com/v.mp4","2026-10-20T06:00:00Z")

    def test_wrong_disconnected_locked_or_paused_channel_stops(self):
        api=Buffer("test-secret")
        for change in [{"service":"instagram"},{"isLocked":True},{"isQueuePaused":True},{"isDisconnected":True}]:
            channel={"service":"tiktok","isLocked":False,"isQueuePaused":False,"isDisconnected":False,**change}
            with patch.object(api,"query",return_value={"channel":channel}):
                with self.assertRaises(WorkflowError):
                    api.preflight("t1")

    def test_scheduled_is_not_published_and_notification_is_blocked(self):
        self.assertFalse(post_summary({"id":"p1","status":"scheduled","schedulingType":"automatic"})["published"])
        self.assertTrue(post_summary({"id":"p1","status":"sent","externalLink":"https://www.tiktok.com/@x/video/1"})["published"])
        for status in ["draft","needs_approval","error"]:
            with self.assertRaises(WorkflowError):
                post_summary({"id":"p1","status":status,"schedulingType":"automatic"})
        with self.assertRaises(WorkflowError):
            post_summary({"id":"p1","status":"scheduled","schedulingType":"notification"})

    def test_no_key_no_endpoint_fallback(self):
        with self.assertRaises(WorkflowError):
            Buffer(None)
        with self.assertRaises(WorkflowError):
            Buffer("key","https://another-service.example/")

    def test_graphql_errors_and_missing_data_fail_closed(self):
        for response in [{"errors":[{"message":"denied"}]},{"data":None}]:
            with patch("tiktok_daily.publishing.json_request",return_value=response):
                with self.assertRaises(WorkflowError):
                    Buffer("key").query("query { account { id } }")


class DuplicateTests(unittest.TestCase):
    def test_committed_reservation_precedes_post_and_retry_never_posts_twice(self):
        cfg=config()
        ledger={"version":1,"days":{}}
        calls=[]
        class FakeGitHub:
            def __init__(self,*args):pass
            def free_only(self):return {}
            def load(self,*args):return copy.deepcopy(ledger),"s0"
            def host(self,*args):return "https://github.com/u/r/releases/download/tag/video.mp4"
            def save(self,path,value,sha):
                ledger.clear();ledger.update(copy.deepcopy(value));calls.append("checkpoint")
                return "s1"
        class FakeBuffer:
            def __init__(self,*args):pass
            def preflight(self,*args):return {"organizationId":"o1"}
            def create(self,*args):
                self.assert_reserved()
                calls.append("create")
                raise WorkflowError("Network timeout after possible server acceptance")
            def assert_reserved(self):
                if next(iter(ledger["days"].values()))["status"]!="submitting":
                    raise AssertionError("Must checkpoint first")
            def find_existing(self,*args):return None
        result={"topic_key":"topic-1","topic":"Example","caption":"original","sha256":"abc123abc123","published":False}
        env={"GITHUB_REPOSITORY":"u/r","GITHUB_TOKEN":"test","GITHUB_REF_NAME":"main","BUFFER_API_KEY":"test","BUFFER_TIKTOK_CHANNEL_ID":"t1"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,env), \
             patch("tiktok_daily.__main__.GitHub",FakeGitHub),patch("tiktok_daily.__main__.Buffer",FakeBuffer), \
             patch("tiktok_daily.__main__.generate",return_value=result),patch("tiktok_daily.__main__.public_media_check"), \
             patch("tiktok_daily.__main__.due_time",return_value="2026-10-20T06:00:00Z"):
            with self.assertRaises(WorkflowError):live(cfg,Path(directory),"live")
            with self.assertRaises(WorkflowError):live(cfg,Path(directory),"live")
        self.assertEqual(calls.count("create"),1)
        self.assertEqual(calls[0],"checkpoint")

    def test_private_repository_is_rejected_before_live_work(self):
        github=GitHub("u/r","test","main")
        with patch("tiktok_daily.publishing.json_request",return_value={"private":True,"visibility":"private"}):
            with self.assertRaisesRegex(WorkflowError,"public repository"):
                github.free_only()


class SubtitleTests(unittest.TestCase):
    def test_monotonic_complete_scene_timing(self):
        cues=subtitles("One two three four five six seven eight nine ten eleven twelve thirteen",4,8)
        self.assertEqual(cues[0]["start"],4)
        self.assertEqual(cues[-1]["end"],12)
        self.assertTrue(all(a["end"]==b["start"] for a,b in zip(cues,cues[1:])))
        self.assertEqual(" ".join(c["text"] for c in cues),"One two three four five six seven eight nine ten eleven twelve thirteen")
        self.assertEqual(timestamp(61.999),"00:01:01,999")


class EditorialTests(unittest.TestCase):
    def fixture(self):
        candidate={"title":"Grand Theft Auto VI", "sources":[{"id":"1","title":"Gaming relationships",
            "excerpt":"Some respondents say that forming romantic relationships with fictional characters can create difficulties between real partners. The survey asked adults about their opinions."}]}
        sentences=[
            "Could your partner see a relationship in a video game as crossing a boundary in real life?",
            "Some people have concerns when fictional romance starts to feel like a connection outside their real relationship.",
            "The discussion is about how couples understand their boundaries when a romantic interest exists inside a game.",
            "Would you see things the same way, or would you draw a different line for your own relationship?"]
        package={"title":"A gaming dilemma","scenes":[{"title":"A gaming question","narration":s,"source_ids":["1"]} for s in sentences],
                 "caption":"Would a fictional romance change how you see your real relationship?","hashtags":["#Zambia","#Gaming","#Explained"]}
        return package,candidate

    def test_placeholder_tags_and_production_labels_are_normalized(self):
        package,candidate=self.fixture()
        package["hashtags"]=["#Topic","#FYP","#Explained"]
        package["scenes"][0]["title"]="Hook:"
        package["scenes"][0]["narration"]+=" #Gaming #Hook"
        finalize_package(package,candidate)
        self.assertIn("#GrandTheftAutoVi",package["hashtags"])
        self.assertNotIn("#Topic",package["hashtags"])
        self.assertNotIn("#FYP",package["hashtags"])
        self.assertEqual(package["scenes"][0]["title"],"The story")
        self.assertTrue(validate_package(package,candidate))

    def test_percentage_claims_are_blocked_even_when_source_contains_them(self):
        package,candidate=self.fixture()
        candidate["sources"][0]["excerpt"]+=" 13% of surveyed men disagree."
        package["scenes"][0]["narration"]+=" About 13% disagree."
        with self.assertRaisesRegex(WorkflowError,"survey statistics"):
            validate_package(package,candidate)

    def test_new_numbers_and_spoken_hashtags_are_blocked(self):
        for extra in [" There are 782 people."," #Gaming"]:
            package,candidate=self.fixture()
            package["scenes"][0]["narration"]+=extra
            with self.assertRaises(WorkflowError):validate_package(package,candidate)


if __name__=="__main__":
    unittest.main()

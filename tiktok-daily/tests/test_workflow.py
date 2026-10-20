import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tiktok_daily.common import WorkflowError, config, due_time, json_request
from tiktok_daily.publishing import Buffer, GitHub, post_summary, public_media_check
from tiktok_daily.research import is_fresh, parse_trends, parse_news, primary_source, source_excerpt, research, enrich
from tiktok_daily.video import subtitles, timestamp
from tiktok_daily.writer import Writer, validate_package, finalize_package
from tiktok_daily.__main__ import live, generate


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
    def test_missing_channel_and_deleted_post_are_reported_without_a_traceback(self):
        api=Buffer("test-secret")
        with patch.object(api,"query",return_value={"channel":None}):
            with self.assertRaisesRegex(WorkflowError,"not found"):
                api.preflight("missing")
        with patch.object(api,"query",return_value={"post":None}):
            with self.assertRaisesRegex(WorkflowError,"no replacement"):
                api.post("deleted")

    def test_invalid_json_is_a_workflow_error(self):
        with patch("tiktok_daily.common.request",return_value=b"<html>Temporarily unavailable</html>"):
            with self.assertRaisesRegex(WorkflowError,"Invalid JSON"):
                json_request("https://api.buffer.com")

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
    def test_status_checks_later_posts_after_uncertain_or_failed_older_posts(self):
        ledger={"version":1,"days":{
            "2026-10-17":{"status":"submission_uncertain","organization_id":"o1","channel_id":"t1","caption":"old"},
            "2026-10-18":{"status":"scheduled","post_id":"failed"},
            "2026-10-19":{"status":"scheduled","post_id":"delivered"}}}
        revisions=["s0"]
        checked=[]
        class FakeGitHub:
            def __init__(self,*args):pass
            def free_only(self):return {}
            def load(self,*args):return copy.deepcopy(ledger),revisions[-1]
            def save(self,path,value,sha):
                if sha!=revisions[-1]:raise AssertionError("Stale ledger SHA after a failed post")
                revisions.append("s"+str(len(revisions)))
                ledger.clear();ledger.update(copy.deepcopy(value))
                return revisions[-1]
            def cleanup(self,*args):pass
        class FakeBuffer:
            def __init__(self,*args):pass
            def preflight(self,*args):return {"organizationId":"o1"}
            def find_existing(self,*args):return None
            def post(self,post_id):
                checked.append(post_id)
                return {"id":post_id,"status":"error" if post_id=="failed" else "sent",
                        "schedulingType":"automatic","error":{"message":"Channel rejected the post"}}
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ,{"BUFFER_TIKTOK_CHANNEL_ID":"t1"}), \
             patch("tiktok_daily.__main__.GitHub",FakeGitHub),patch("tiktok_daily.__main__.Buffer",FakeBuffer):
            with self.assertRaisesRegex(WorkflowError,"remaining posts were still checked"):
                live(config(),Path(directory),"status")
            result=json.loads((Path(directory)/"result.json").read_text())
        self.assertEqual(checked,["failed","delivered"])
        self.assertEqual(len(result["posts"]),3)
        self.assertTrue(result["posts"][-1]["published"])
        self.assertEqual(ledger["days"]["2026-10-18"]["status"],"error")
        self.assertEqual(ledger["days"]["2026-10-19"]["status"],"sent")

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


class ResearchTests(unittest.TestCase):
    def test_usable_primary_account_takes_priority_over_secondary_headlines(self):
        primary={"url":"https://www.nasa.gov/story/","primary":True}
        secondary={"url":"https://example.com/story/","primary":False}
        candidate={"title":"NASA simulation","sources":[secondary,primary]}
        extracted={**primary,"domain":"www.nasa.gov","excerpt":"The NASA simulation takes place in a habitat on Earth."}
        with patch("tiktok_daily.research.source_excerpt",return_value=extracted) as fetch:
            result=enrich(candidate)
        fetch.assert_called_once_with(primary)
        self.assertEqual(result["sources"],[{**extracted,"id":"1"}])

    def test_primary_trend_precedes_secondary_trend_with_same_region_priority(self):
        cfg=config();cfg["trend_geos"]=["US"]
        cfg["news_feeds"]=[f for f in cfg["news_feeds"] if f["primary"]]
        def item(title,url,date):
            return f'''<item><title>{title}</title><pubDate>{date}</pubDate>
              <ht:news_item><ht:news_item_title>{title}</ht:news_item_title>
              <ht:news_item_url>{url}</ht:news_item_url><ht:news_item_source>Publisher</ht:news_item_source>
              </ht:news_item></item>'''
        xml='<rss xmlns:ht="https://trends.google.com/trending/rss"><channel>'
        xml+=item("NASA","https://www.nasa.gov/story/","Tue, 20 Oct 2026 04:00:00 GMT")
        xml+=item("Football","https://example.com/story/","Tue, 20 Oct 2026 05:00:00 GMT")
        xml+='</channel></rss>'
        with patch("tiktok_daily.research.request",side_effect=lambda url,**kw:xml.encode() if 'trends.google.com' in url else b'<rss><channel/></rss>'):
            candidates,_=research(cfg,datetime(2026,10,20,6,tzinfo=timezone.utc))
        self.assertEqual(candidates[0]["title"],"NASA")

    def test_trend_primary_publisher_uses_host_not_display_name(self):
        cfg=config()
        self.assertTrue(primary_source("https://www.nasa.gov/science/story/",cfg))
        self.assertTrue(primary_source("https://science.nasa.gov/story/",cfg))
        for url in ["https://nasa.gov.example.com/story","https://fake-nasa.gov/story","http://nasa.gov/story","https://nasa.gov@evil.example/story"]:
            self.assertFalse(primary_source(url,cfg))
        xml='''<rss xmlns:ht="https://trends.google.com/trending/rss"><channel><item>
          <title>NASA</title><pubDate>Tue, 20 Oct 2026 05:00:00 GMT</pubDate>
          <ht:news_item><ht:news_item_title>NASA science news</ht:news_item_title>
          <ht:news_item_url>https://www.nasa.gov/science/story/</ht:news_item_url>
          <ht:news_item_source>NASA</ht:news_item_source></ht:news_item></item></channel></rss>'''
        items=parse_trends(xml,"US",cfg,datetime(2026,10,20,6,tzinfo=timezone.utc))
        self.assertTrue(items[0]["sources"][0]["primary"])

    def test_primary_feed_context_keeps_fresh_science_titles(self):
        feed=next(f for f in config()["news_feeds"] if f["name"]=="NASA")
        xml='''<rss><channel><item><title>Watching the Clouds Go By</title>
          <pubDate>Tue, 20 Oct 2026 05:00:00 GMT</pubDate>
          <link>https://www.nasa.gov/story/</link><description>A science story.</description></item></channel></rss>'''
        self.assertEqual(len(parse_news(xml,feed,config(),datetime(2026,10,20,6,tzinfo=timezone.utc))),1)

    def test_accessible_primary_feed_summary_survives_article_outage(self):
        source={"url":"https://www.nasa.gov/story/","primary":True,
                "feed_summary":" ".join(["A spacecraft must survive vibration and shock during launch."]*5)}
        with patch("tiktok_daily.research.request",side_effect=WorkflowError("HTTP 503 from www.nasa.gov")):
            result=source_excerpt(source)
            self.assertEqual(result["evidence_origin"],"primary_feed_summary")
            with self.assertRaises(WorkflowError):source_excerpt({**source,"primary":False})

    def test_source_outages_do_not_exhaust_ai_retry_budget(self):
        sources=[{"id":"1","title":"Spacecraft testing","url":"https://www.nasa.gov/story/",
                  "publisher":"NASA","domain":"www.nasa.gov","primary":True,"excerpt":"Evidence for a script."}]
        candidates=[{"title":"Unavailable "+str(i),"sources":[{"url":"https://example.com/"}]} for i in range(6)]
        candidates.append({"title":"NASA testing","sources":sources,"key":"topic1","signal":"Fresh news"})
        def fake_enrich(raw):raise WorkflowError("HTTP 503 from publisher.example")
        package={"title":"Spacecraft testing","scenes":[],"caption":"Why test spacecraft?","hashtags":["#Zambia","#Space","#NASA"]}
        with tempfile.TemporaryDirectory() as directory, \
             patch("tiktok_daily.__main__.research",return_value=(candidates,[])), \
             patch("tiktok_daily.__main__.enrich",side_effect=fake_enrich), \
             patch("tiktok_daily.__main__.Writer") as writer, \
             patch("tiktok_daily.__main__.render",return_value={"decoded_ok":True}), \
             patch("tiktok_daily.__main__.sha256",return_value="hash"):
            writer.return_value.write.return_value=package
            writer.return_value.verify.return_value={"supported":True}
            result=generate(config(),Path(directory),datetime(2026,10,20,6,tzinfo=timezone.utc))
        self.assertEqual(result["topic"],"NASA testing")
        writer.return_value.write.assert_called_once()


class EditorialTests(unittest.TestCase):
    def test_questions_and_captions_are_checked_for_unsupported_premises(self):
        writer=Writer.__new__(Writer)
        package={"title":"Spacecraft testing","caption":"Why did NASA cancel the launch?",
                 "scenes":[{"title":"Your view","narration":"Would you try this?","source_ids":["1"]}]}
        candidate={"sources":[{"id":"1","title":"NASA tests spacecraft","publisher":"NASA","excerpt":"NASA tests spacecraft vibrations."}]}
        with patch.object(writer,"json",side_effect=[{"supported":True,"reason":"Opinion question"},
                                                     {"supported":False,"reason":"No launch cancellation in evidence"}]) as check:
            with self.assertRaisesRegex(WorkflowError,"Video title and caption"):
                writer.verify(package,candidate)
        self.assertIn("Would you try this?",check.call_args_list[0].args[1])
        self.assertIn("Why did NASA cancel",check.call_args_list[1].args[1])

    def test_cleanup_preserves_qualifications_after_dashes(self):
        package,candidate=self.fixture()
        package["scenes"][0]["narration"]="A future mission is planned — it has not been launched."
        finalize_package(package,candidate)
        self.assertIn("it has not been launched",package["scenes"][0]["narration"])

    def test_simulation_crew_are_not_promoted_to_astronauts(self):
        package,candidate=self.fixture()
        candidate["sources"][0]["excerpt"]="Four crew members worked inside an Earth-based simulated Mars habitat."
        package["scenes"][0]["narration"]="Four astronauts spent a year in an isolated habitat."
        with self.assertRaisesRegex(WorkflowError,"role stated"):
            validate_package(package,candidate)
        package["scenes"][0]["narration"]="The crew worked on a Martian surface."
        with self.assertRaisesRegex(WorkflowError,"simulation qualifier"):
            validate_package(package,candidate)

    def test_durations_are_omitted_even_when_the_number_occurs_in_evidence(self):
        package,candidate=self.fixture()
        candidate["sources"][0]["excerpt"]+=" He made his first start in 18 months and scored."
        package["scenes"][0]["narration"]="The player scored after 18 months."
        with self.assertRaisesRegex(WorkflowError,"numerical durations"):
            validate_package(package,candidate)

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

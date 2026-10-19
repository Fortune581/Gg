import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import WorkflowError, json_request, request

CREATE_POST = """mutation DailyVideo($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status dueAt schedulingType externalLink } }
    ... on MutationError { message }
  }
}"""
POST_QUERY = """query DailyVideoStatus($input: PostInput!) {
  post(input: $input) { id status dueAt schedulingType externalLink error { message } }
}"""


class Buffer:
    def __init__(self, token, endpoint="https://api.buffer.com"):
        if not token:
            raise WorkflowError("BUFFER_API_KEY is missing; no publishing attempt was made")
        if endpoint != "https://api.buffer.com":
            raise WorkflowError("Only Buffer's official API endpoint is permitted")
        self.token, self.endpoint = token, endpoint

    def query(self, query, variables=None):
        data=json_request(self.endpoint,data={"query":query,"variables":variables or {}},
                          headers={"Authorization":"Bearer " + self.token},timeout=45)
        if data.get("errors"):
            messages="; ".join(str(e.get("message","GraphQL error")) for e in data["errors"])
            raise WorkflowError("Buffer GraphQL rejected the request: " + messages[:700])
        if not isinstance(data.get("data"),dict):
            raise WorkflowError("Buffer returned an unexpected response")
        return data["data"]

    def channels(self):
        account=self.query("query { account { organizations { id name } } }")
        channels=[]
        for org in account["account"]["organizations"]:
            data=self.query("""query TikTokChannels($input: ChannelsInput!) {
                channels(input:$input) { id name service organizationId isDisconnected isLocked isQueuePaused }
            }""",{"input":{"organizationId":org["id"]}})
            channels.extend(data["channels"])
        return channels

    def preflight(self,channel_id):
        data=self.query("""query TikTokConnection($input: ChannelInput!) {
          channel(input:$input) { id name service organizationId isDisconnected isLocked isQueuePaused }
        }""",{"input":{"id":channel_id}})
        channel=data["channel"]
        if channel["service"]!="tiktok":
            raise WorkflowError("Selected Buffer channel is not a TikTok account")
        if channel["isDisconnected"] or channel["isLocked"] or channel["isQueuePaused"]:
            raise WorkflowError("TikTok channel is disconnected, locked, or paused in Buffer")
        return channel

    def create(self,channel_id,text,media_url,due_at):
        input_value={"channelId":channel_id,"text":text,"schedulingType":"automatic",
                     "mode":"customScheduled","dueAt":due_at,"needsApproval":False,
                     "aiAssisted":True,"assets":[{"video":{"url":media_url,"metadata":{"thumbnailOffset":2000}}}],
                     "metadata":{"tiktok":{"isAiGenerated":True}}}
        result=self.query(CREATE_POST,{"input":input_value})["createPost"]
        if result.get("__typename")!="PostActionSuccess" or not result.get("post",{}).get("id"):
            raise WorkflowError("Buffer rejected post creation: " + str(result.get("message","unknown error"))[:600])
        post=result["post"]
        if post.get("schedulingType")!="automatic":
            raise WorkflowError("Buffer returned notification publishing; manual action is required")
        return post

    def post(self,post_id):
        return self.query(POST_QUERY,{"input":{"id":post_id}})["post"]

    def find_existing(self,org_id,channel_id,text):
        cursor=None
        for _ in range(5):
            result=self.query("""query RecoverDailyVideo($input: PostsInput!, $after: String) {
              posts(first:100,after:$after,input:$input) {
                edges { node { id text status dueAt schedulingType externalLink } }
                pageInfo { hasNextPage endCursor }
              }
            }""",{"input":{"organizationId":org_id,"filter":{"channelIds":[channel_id]}},"after":cursor})["posts"]
            for edge in result.get("edges") or []:
                if edge["node"]["text"]==text:
                    return edge["node"]
            page=result["pageInfo"]
            if not page["hasNextPage"]:
                break
            cursor=page["endCursor"]
        return None


class GitHub:
    def __init__(self,repository,token,ref):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",repository or "") or not token:
            raise WorkflowError("GITHUB_REPOSITORY and GITHUB_TOKEN are required for live publishing")
        self.repository,self.token,self.ref=repository,token,ref
        self.api="https://api.github.com/repos/"+repository
        self.headers={"Authorization":"Bearer "+token,"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}

    def free_only(self):
        repo=json_request(self.api,headers=self.headers)
        if repo.get("private") or repo.get("visibility")!="public":
            raise WorkflowError("$0 mode requires a public repository and standard GitHub Actions runners")
        if self.ref!=repo["default_branch"]:
            raise WorkflowError("Live publishing is restricted to the repository's default branch")
        return repo

    def load(self,path):
        url=self.api+"/contents/"+path+"?ref="+urllib.parse.quote(self.ref,safe="")
        try:
            data=json_request(url,headers=self.headers)
        except WorkflowError as exc:
            if str(exc).startswith("HTTP 404 "):
                return {"version":1,"days":{}},None
            raise
        return json.loads(base64.b64decode(data["content"])),data["sha"]

    def save(self,path,ledger,sha):
        value={"message":"chore: checkpoint daily TikTok state [skip ci]","branch":self.ref,
               "content":base64.b64encode((json.dumps(ledger,indent=2)+"\n").encode()).decode()}
        if sha:
            value["sha"]=sha
        result=json_request(self.api+"/contents/"+path,method="PUT",data=value,headers=self.headers)
        return result["content"]["sha"]

    def host(self,video,day):
        # Stable public release URLs remain reachable when Buffer fetches the video later.
        tag="daily-tiktok-"+day
        try:
            release=json_request(self.api+"/releases/tags/"+tag,headers=self.headers)
        except WorkflowError as exc:
            if not str(exc).startswith("HTTP 404 "):
                raise
            release=json_request(self.api+"/releases",method="POST",headers=self.headers,
                                 data={"tag_name":tag,"target_commitish":self.ref,"name":"Daily TikTok "+day,
                                       "body":"Original AI-narrated daily explainer generated by this repository.",
                                       "draft":False,"prerelease":False})
        for asset in release["assets"]:
            if asset["name"]=="video.mp4":
                return asset["browser_download_url"]
        url=release["upload_url"].split("{")[0]+"?name=video.mp4"
        if urllib.parse.urlsplit(url).hostname!="uploads.github.com":
            raise WorkflowError("Unexpected release upload destination")
        asset=json_request(url,method="POST",data=Path(video).read_bytes(),timeout=90,
                           headers={**self.headers,"Content-Type":"video/mp4"})
        return asset["browser_download_url"]

    def cleanup(self,ledger,now,retention_days):
        # Only generated media for confirmed sent posts is eligible. Pending media is retained.
        cutoff=(now-timedelta(days=retention_days)).date().isoformat()
        for day,entry in ledger["days"].items():
            if day>=cutoff or entry.get("status")!="sent" or entry.get("media_removed"):
                continue
            try:
                release=json_request(self.api+"/releases/tags/"+entry["release_tag"],headers=self.headers)
                for asset in release["assets"]:
                    if asset["name"]=="video.mp4":
                        request(self.api+"/releases/assets/"+str(asset["id"]),method="DELETE",headers=self.headers)
                entry["media_removed"]=True
            except WorkflowError:
                # Cleanup cannot trigger a second post or hide a publishing outcome.
                continue


def public_media_check(url):
    # Never send the GitHub or Buffer credential to the public media host or its redirect.
    if not url.startswith("https://github.com/") or "/releases/download/" not in url:
        raise WorkflowError("Expected a stable public GitHub Release asset URL")
    result=request(url,headers={"Range":"bytes=0-31"},limit=50_000_000,timeout=45)
    if len(result)<12 or result[4:8]!=b"ftyp":
        raise WorkflowError("Public media URL did not return an MP4 file")


def post_summary(post):
    status=post.get("status","unknown")
    if status=="sent":
        return {"status":"sent","published":True,"post_id":post["id"],"url":post.get("externalLink")}
    if status=="error":
        raise WorkflowError("TikTok publication failed: "+str((post.get("error") or {}).get("message","unknown error")))
    if post.get("schedulingType")!="automatic":
        raise WorkflowError("Post needs manual notification publishing")
    if status in ("draft","needs_approval"):
        raise WorkflowError("Buffer saved a draft or approval request; nothing has been published")
    return {"status":status,"published":False,"post_id":post["id"],"due_at":post.get("dueAt")}

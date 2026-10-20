import argparse
import hashlib
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from .common import ROOT, WorkflowError, config, due_time, dump, now_local, sha256
from .publishing import Buffer, GitHub, post_summary, public_media_check
from .research import enrich, research
from .video import render
from .writer import Writer, validate_package


def caption(package,candidate):
    text=package["caption"]+"\n\n"+" ".join(package["hashtags"])+"\nAI-generated voice."
    for source in candidate["sources"]:
        addition="\nSource: "+source["url"]
        if len(text+addition)<=2000:
            text+=addition
    return text


def generate(cfg,out,now,seen=(),candidate_file=None,package_file=None):
    if candidate_file:
        candidate=json.loads(Path(candidate_file).read_text())
        candidates=[candidate]
        diagnostics=[{"recorded_evidence":str(candidate_file)}]
    else:
        candidates,diagnostics=research(cfg,now,seen)
    dump(out/"research.json",{"at":now.isoformat(),"feeds":diagnostics,"candidates":candidates})
    if not candidates:
        raise WorkflowError("No fresh eligible topics were available; no stale or invented trend was substituted")
    writer=None
    failures=[]
    script_failures=0
    for raw in candidates:
        stage="sources"
        try:
            candidate=raw if all(s.get("excerpt") for s in raw["sources"]) else enrich(raw)
            stage="generation"
            print("Writing original script:",candidate["title"],flush=True)
            if package_file:
                package=json.loads(Path(package_file).read_text())
                validate_package(package,candidate)
                verification={"supported":None,"reason":"Supplied package; render-only verification"}
            else:
                if writer is None:
                    writer=Writer(cfg)
                package=writer.write(candidate,cfg)
                for check in range(2):
                    try:
                        verification=writer.verify(package,candidate)
                        break
                    except WorkflowError as issue:
                        if check:
                            raise
                        print("Revising the AI script after an evidence-check failure",flush=True)
                        package=writer.revise(package,candidate,str(issue))
            dump(out/"candidate.json",candidate)
            dump(out/"sources.json",[{k:s[k] for k in ("id","title","url","publisher","domain","primary")}
                 | {"evidence_sha256":hashlib.sha256(s["excerpt"].encode()).hexdigest()} for s in candidate["sources"]])
            dump(out/"script.json",package)
            dump(out/"evidence-check.json",verification)
            text=caption(package,candidate)
            (out/"caption.txt").write_text(text+"\n")
            print("Rendering neural voice, animated scenes, and subtitles",flush=True)
            media=render(package,candidate,out,cfg,now.date().isoformat())
            result={"day":now.date().isoformat(),"timezone":cfg["timezone"],"topic":candidate["title"],
                    "topic_key":candidate["key"],"signal":candidate["signal"],"script_model":cfg["model_name"],
                    "voice":"local Piper LJ Speech high", "caption":text,"media":media,
                    "sha256":sha256(out/"video.mp4"),"published":False,"status":"generated",
                    "generation_service_cost_usd":0,"evidence_check":verification}
            dump(out/"result.json",result)
            return result
        except WorkflowError as exc:
            failures.append({"topic":raw["title"],"stage":stage,"reason":str(exc)})
            dump(out/"rejected-topics.json",failures)
            if writer is not None and getattr(writer,"attempts",None):
                dump(out/"script-attempts.json",writer.attempts)
            if writer is not None and getattr(writer,"last_raw",None):
                (out/"last-model-output.txt").write_text(writer.last_raw)
            print("Skipped topic:",str(exc),flush=True)
            script_failures += stage != "sources"
            # Unavailable publishers must not consume the expensive AI retry budget
            # or prevent later first-party feed candidates from being attempted.
            if package_file or script_failures>=3:
                break
    raise WorkflowError("No topic passed source, script, and media checks. See rejected-topics.json")


def check_existing(buffer,github,ledger,sha,cfg,day):
    entry=ledger["days"][day]
    post_id=entry.get("post_id")
    if post_id:
        post=buffer.post(post_id)
    else:
        post=buffer.find_existing(entry["organization_id"],entry["channel_id"],entry["caption"])
        if post is None:
            raise WorkflowError("A prior submission has an uncertain outcome. No retry was made: inspect Buffer before clearing this day")
    entry.update({"post_id":post["id"],"status":post["status"],"last_checked":now_local(cfg).isoformat()})
    sha=github.save(cfg["state_path"],ledger,sha)
    return {**post,"day":day,"existing_post":True},sha


def live(cfg,out,mode):
    # No provider inference or paid fallback endpoint exists in this implementation.
    github=GitHub(os.environ.get("GITHUB_REPOSITORY"),os.environ.get("GITHUB_TOKEN"),os.environ.get("GITHUB_REF_NAME","main"))
    github.free_only()
    buffer=Buffer(os.environ.get("BUFFER_API_KEY"),cfg["buffer_endpoint"])
    channel_id=os.environ.get("BUFFER_TIKTOK_CHANNEL_ID","")
    if not channel_id:
        raise WorkflowError("BUFFER_TIKTOK_CHANNEL_ID is missing; run the Buffer preflight to choose your account")
    channel=buffer.preflight(channel_id)
    ledger,ledger_sha=github.load(cfg["state_path"])
    now=now_local(cfg)
    day=now.date().isoformat()
    if mode=="status":
        results=[]
        for saved_day,entry in ledger["days"].items():
            if entry.get("status") in ("sent","error"):
                continue
            try:
                post,ledger_sha=check_existing(buffer,github,ledger,ledger_sha,cfg,saved_day)
                results.append({**post_summary(post),"day":saved_day,"existing_post":True})
            except WorkflowError as exc:
                results.append({"day":saved_day,"status":"check_failed","error":str(exc),"published":None})
        github.cleanup(ledger,now,cfg["retention_days"])
        github.save(cfg["state_path"],ledger,ledger_sha)
        result={"status":"status_checked","posts":results,"published":any(r.get("published") for r in results)}
        dump(out/"result.json",result)
        if any(r.get("error") for r in results):
            raise WorkflowError("Some delivery checks failed; all remaining posts were still checked. See result.json")
        return result
    if day in ledger["days"]:
        post,_=check_existing(buffer,github,ledger,ledger_sha,cfg,day)
        result={**post_summary(post),"day":day,"existing_post":True}
        dump(out/"result.json",result)
        return result
    cutoff=(now-timedelta(days=cfg["repeat_window_days"])).date().isoformat()
    seen={e["topic_key"] for d,e in ledger["days"].items() if d>=cutoff and e.get("topic_key")}
    generated=generate(cfg,out,now,seen)
    media_url=github.host(out/"video.mp4",day+"-"+generated["sha256"][:10])
    public_media_check(media_url)
    due_at=due_time(day,cfg)
    entry={"status":"submitting","topic_key":generated["topic_key"],"topic":generated["topic"],
           "caption":generated["caption"],"channel_id":channel_id,"organization_id":channel["organizationId"],
           "media_url":media_url,"release_tag":"daily-tiktok-"+day+"-"+generated["sha256"][:10],
           "due_at":due_at,"run_id":os.environ.get("GITHUB_RUN_ID"),"sha256":generated["sha256"]}
    ledger["days"][day]=entry
    # This compare-and-swap commit MUST succeed before Buffer is called.
    ledger_sha=github.save(cfg["state_path"],ledger,ledger_sha)
    try:
        post=buffer.create(channel_id,generated["caption"],media_url,due_at)
    except WorkflowError:
        entry["status"]="submission_uncertain"
        github.save(cfg["state_path"],ledger,ledger_sha)
        raise
    entry.update({"post_id":post["id"],"status":post["status"]})
    ledger_sha=github.save(cfg["state_path"],ledger,ledger_sha)
    result={**generated,**post_summary(post),"media_url":media_url}
    dump(out/"result.json",result)
    return result


def main():
    parser=argparse.ArgumentParser(description="Free daily original TikTok video workflow")
    parser.add_argument("mode",choices=["preview","live","status","preflight"])
    parser.add_argument("--output",default="output/latest")
    parser.add_argument("--candidate",help="Recorded research candidate for a repeatable local preview")
    parser.add_argument("--package",help="Previously generated script for a render-only preview")
    args=parser.parse_args()
    cfg=config()
    out=Path(args.output).resolve()
    out.mkdir(parents=True,exist_ok=True)
    try:
        if args.mode=="preflight":
            buffer=Buffer(os.environ.get("BUFFER_API_KEY"),cfg["buffer_endpoint"])
            result={"channels":[c for c in buffer.channels() if c["service"]=="tiktok"],"published":False}
            if os.environ.get("BUFFER_TIKTOK_CHANNEL_ID"):
                result["selected"]=buffer.preflight(os.environ["BUFFER_TIKTOK_CHANNEL_ID"])
            dump(out/"preflight.json",result)
        elif args.mode=="preview":
            result=generate(cfg,out,now_local(cfg),candidate_file=args.candidate,package_file=args.package)
        else:
            if args.candidate or args.package:
                raise WorkflowError("Live publishing must use newly researched, AI-checked evidence")
            result=live(cfg,out,args.mode)
        print(json.dumps(result,indent=2,ensure_ascii=False),flush=True)
        summary=os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary,"a") as stream:
                stream.write("\nDaily TikTok result\n\n```json\n"+json.dumps(result,indent=2)+"\n```\n")
        return 0
    except WorkflowError as exc:
        dump(out/"error.json",{"status":"failed","error":str(exc),"published":None if args.mode in ("live","status") else False,
                               "note":"Inspect the durable ledger if a Buffer submission was attempted."})
        print("Workflow stopped:",str(exc),file=sys.stderr,flush=True)
        return 1


if __name__=="__main__":
    raise SystemExit(main())

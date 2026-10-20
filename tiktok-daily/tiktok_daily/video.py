import json
import math
import re
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

from .common import WorkflowError, dump, model_dir


def run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise WorkflowError("Media command failed: " + result.stderr[-1800:])
    return result.stdout


def duration(path):
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def subtitles(text, start, seconds, words_per_cue=6):
    words = text.split()
    groups = [words[i:i+words_per_cue] for i in range(0, len(words), words_per_cue)]
    # The scene boundary is exact. Word timing within each cue is estimated.
    total = sum(max(len(w), 2) for w in words)
    cursor = start
    cues = []
    for group in groups:
        span = seconds * sum(max(len(w), 2) for w in group) / total
        cues.append({"start": round(cursor, 3), "end": round(cursor+span, 3), "text": " ".join(group)})
        cursor += span
    return cues


def timestamp(seconds, ass=False):
    scale = 100 if ass else 1000
    ticks = round(seconds * scale)
    hours, remainder = divmod(ticks, 3600*scale)
    minutes, remainder = divmod(remainder, 60*scale)
    secs, fraction = divmod(remainder, scale)
    return f"{hours}:{minutes:02}:{secs:02}.{fraction:02}" if ass else f"{hours:02}:{minutes:02}:{secs:02},{fraction:03}"


def write_subtitles(cues, out, cfg):
    srt = "\n\n".join(f"{i}\n{timestamp(c['start'])} --> {timestamp(c['end'])}\n{c['text']}" for i,c in enumerate(cues,1))
    (out / "subtitles.srt").write_text(srt+"\n")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {cfg['width']}
PlayResY: {cfg['height']}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,56,&H00FFFFFF,&H0000FFFF,&H00110B06,&H90110B06,-1,0,0,0,100,100,0,0,1,4,1,2,95,190,395,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for cue in cues:
        text = cue["text"].replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ")
        lines.append(f"Dialogue: 0,{timestamp(cue['start'],True)},{timestamp(cue['end'],True)},Default,,0,0,0,,{text}")
    (out / "subtitles.ass").write_text(header+"\n".join(lines)+"\n")


@lru_cache(maxsize=48)
def font(size, bold=False):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/" + name, size)


def wrap_pixels(draw, text, face, width):
    lines, current = [], ""
    for word in text.split():
        trial = (current + " " + word).strip()
        if current and draw.textlength(trial, font=face) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def story_visual_kind(scene, candidate):
    from .writer import VISUALS
    if scene.get("visual") in VISUALS:
        return scene["visual"]
    # The scene's subject wins over the overall topic when choosing an illustration.
    groups = (("funding", ("funding", "grant", "budget", "money", "finance")),
              ("people", ("appoint", "administrator", "staff", "role", "official")),
              ("rights", ("settlement", "investigation", "civil rights", "agreement", "court", "law")),
              ("campus", ("university", "school", "college", "campus", "student")),
              ("science", ("nasa", "space", "planet", "spacecraft", "science")),
              ("sport", ("football", "soccer", "rugby", "sport", "match")),
              ("technology", ("phone", "technology", "tech", "artificial intelligence")),
              ("music", ("music", "concert", "song", "festival")))
    for words in (scene.get("title", "")+" "+scene.get("narration", ""), candidate.get("title", "")):
        for kind, terms in groups:
            if any(x in words.lower() for x in terms):
                return kind
    return "world"


def illustration(kind, accent, ink):
    """Draw an original, full-scene illustration and a separate moving prop."""
    from PIL import Image, ImageDraw
    art = Image.new("RGBA", (900, 680))
    d = ImageDraw.Draw(art)
    navy, blue, cream = "#20365A", "#6D9CC6", "#F1EEDF"
    prop = Image.new("RGBA", (220, 220))
    q = ImageDraw.Draw(prop)
    if kind == "campus":
        d.ellipse((30, 516, 870, 650), fill="#14294C")
        d.polygon([(85,300),(420,135),(815,310),(480,490)],fill=accent)
        d.polygon([(85,300),(480,490),(480,607),(85,416)],fill=blue)
        d.polygon([(480,490),(815,310),(815,426),(480,607)],fill=navy)
        for x,y in ((164,365),(258,410),(352,455)):
            d.polygon([(x,y),(x+51,y+24),(x+51,y+91),(x,y+66)],fill=cream)
        for x,y in ((541,484),(641,430),(737,378)):
            d.polygon([(x,y),(x+38,y-21),(x+38,y+30),(x,y+52)],fill="#A8DCCB")
        d.polygon([(342,286),(436,238),(540,286),(445,335)],fill=cream)
        d.line((435,236,435,131),fill=cream,width=8)
        d.polygon([(440,132),(537,157),(440,190)],fill=accent)
        q.ellipse((72,23,133,84),fill=accent)
        q.rounded_rectangle((49,95,155,198),radius=40,fill=cream)
    elif kind == "rights":
        d.polygon([(106,166),(580,94),(762,486),(286,585)],fill="#2A3D60")
        d.rounded_rectangle((230,72,694,593),radius=35,fill=cream)
        d.polygon([(570,72),(694,196),(570,196)],fill="#B9CCCB")
        d.rounded_rectangle((281,139,483,167),radius=9,fill=navy)
        for i,length in enumerate((291,249,291,197,276,223)):
            d.rounded_rectangle((281,236+i*44,281+length,248+i*44),radius=6,fill="#94A7B4")
        d.rounded_rectangle((281,537,456,554),radius=6,fill=accent)
        q.ellipse((12,12,206,206),fill=accent)
        q.line((61,106,94,139,158,70),fill=navy,width=18)
    elif kind == "funding":
        d.ellipse((45,524,852,650),fill="#14294C")
        d.rounded_rectangle((131,138,573,502),radius=32,fill=cream)
        d.rounded_rectangle((166,184,395,211),radius=10,fill=navy)
        for i in range(4):
            d.rounded_rectangle((167,262+i*43,457-i*20,275+i*43),radius=6,fill="#A1B2BE")
        for x,top,count in ((506,451,3),(622,388,4),(736,472,2)):
            for row in range(count):
                y=top-row*36
                d.rounded_rectangle((x,y,x+112,y+42),radius=18,fill="#D6A547",outline="#F7E4A9",width=3)
                d.ellipse((x,y-8,x+112,y+23),fill=accent,outline=cream,width=3)
        q.ellipse((16,16,199,199),fill=accent,outline=cream,width=8)
        q.ellipse((61,61,154,154),outline=navy,width=9)
    elif kind == "people":
        d.line((200,257,450,154,700,257),fill=blue,width=8)
        for x,y,col in ((114,200,blue),(353,92,accent),(591,200,"#B8A6E1")):
            d.ellipse((x+34,y,x+155,y+121),fill=cream)
            d.rounded_rectangle((x,y+145,x+190,y+389),radius=75,fill=col)
            d.rounded_rectangle((x+55,y+185,x+134,y+210),radius=8,fill=navy)
        q.rounded_rectangle((8,22,210,159),radius=28,fill=cream)
        q.polygon([(42,153),(43,207),(96,155)],fill=cream)
        for y in (64,97): q.rounded_rectangle((42,y,177,y+12),radius=6,fill=navy)
    elif kind == "science":
        d.ellipse((55,62,525,532),fill="#284B78")
        d.arc((8,177,661,435),6,354,fill=accent,width=28)
        d.ellipse((130,139,450,459),fill="#476C9C")
        d.ellipse((192,187,278,273),fill="#7093B9")
        d.ellipse((341,301,400,360),fill="#243C65")
        # A stylized satellite; it is an illustration, not an image of a real mission.
        d.polygon([(473,374),(613,303),(704,376),(566,447)],fill=cream)
        d.polygon([(566,447),(704,376),(704,454),(566,526)],fill=navy)
        for dx in (-230,185):
            d.polygon([(530+dx,367),(660+dx,302),(745+dx,370),(614+dx,436)],fill=blue,outline=cream,width=3)
        d.line((622,329,654,240),fill=cream,width=8)
        d.arc((606,201,706,269),170,355,fill=accent,width=8)
        q.polygon([(110,10),(135,78),(208,110),(135,139),(110,208),(81,138),(10,110),(80,79)],fill=accent)
    elif kind == "technology":
        d.polygon([(100,172),(619,94),(805,483),(286,594)],fill="#23385B")
        d.rounded_rectangle((290,31,636,618),radius=55,fill=cream)
        d.rounded_rectangle((314,96,611,536),radius=18,fill=navy)
        d.rounded_rectangle((401,58,517,74),radius=7,fill=navy)
        for i,col in enumerate((accent,blue,"#B8A6E1")):
            d.rounded_rectangle((345,137+i*114,576,217+i*114),radius=18,fill=col)
            d.ellipse((365,156+i*114,406,197+i*114),fill=cream)
            d.line((425,165+i*114,548,165+i*114),fill=navy,width=8)
            d.line((425,184+i*114,514,184+i*114),fill=navy,width=7)
        d.ellipse((448,558,482,592),fill=navy)
        q.rounded_rectangle((8,40,210,177),radius=25,fill=accent)
        q.polygon([(151,170),(188,209),(185,156)],fill=accent)
        for x in (49,100,151): q.ellipse((x,91,x+24,115),fill=navy)
    elif kind == "sport":
        d.ellipse((15,80,884,627),fill="#274665",outline=cream,width=5)
        d.polygon([(219,208),(687,208),(833,525),(73,525)],fill="#34756B",outline=cream,width=5)
        d.line((453,211,453,523),fill=cream,width=5)
        d.ellipse((347,308,560,451),outline=cream,width=5)
        for x in (139,630): d.rectangle((x,315,x+140,438),outline=cream,width=5)
        for x in range(95,810,55): d.line((x,145,x+17,163),fill=accent,width=7)
        q.ellipse((14,14,205,205),fill=cream,outline=navy,width=5)
        q.polygon([(110,61),(162,98),(143,158),(78,158),(58,98)],fill=navy)
    elif kind == "music":
        d.ellipse((154,98,728,636),fill=navy)
        d.ellipse((214,151,672,589),fill="#365575",outline=cream,width=5)
        for r in (83,141,195): d.ellipse((447-r,370-r,447+r,370+r),outline=blue,width=4)
        d.ellipse((395,317,499,421),fill=accent)
        d.arc((198,38,701,492),180,359,fill=cream,width=30)
        d.rounded_rectangle((160,245,263,464),radius=35,fill=accent)
        d.rounded_rectangle((638,245,741,464),radius=35,fill=accent)
        q.line((73,72,73,171,170,138,170,40,73,72),fill=accent,width=17)
        q.ellipse((17,145,88,200),fill=accent); q.ellipse((114,113,184,166),fill=accent)
    else:
        d.ellipse((153,58,749,654),fill="#2B5A78",outline=cream,width=5)
        for x in (256,354): d.ellipse((x,58,902-x,654),outline=blue,width=4)
        for y in (184,294,414): d.arc((153,y,749,712-y//4),0,180,fill=blue,width=4)
        d.polygon([(323,176),(443,148),(509,232),(448,287),(461,378),(388,428),(330,340),(277,294)],fill=accent)
        d.polygon([(559,364),(645,405),(661,484),(588,565),(543,484)],fill="#A6C8A6")
        q.ellipse((37,17,181,162),fill=accent)
        q.polygon([(49,125),(108,216),(169,125)],fill=accent)
        q.ellipse((83,64,136,117),fill=navy)
    return art, prop


CHAPTERS = ("THE STORY", "THE BACKGROUND", "WHAT CHANGED", "A CLOSER LOOK", "WHY IT MATTERS", "THE IMPORTANT LIMIT", "WHERE IT LEAVES US", "YOUR TAKE")
PALETTES = (("#101D35","#D7F66E","#F5F2E8"),("#172C4C","#96DCEA","#F5F2E8"),
            ("#E7EFE8","#185B52","#132B39"),("#2D233D","#FFB991","#FBF0E5"),
            ("#122F3B","#A8EFCC","#F5F2E8"),("#F4E9DB","#9F4C42","#293149"),
            ("#192E31","#D7F66E","#F5F2E8"),("#2533A4","#D7F66E","#F8F1DF"))


def scene_assets(scene, package, candidate, index, cfg, day):
    from PIL import Image, ImageDraw
    w,h=cfg["width"],cfg["height"]
    bg_color,accent,ink=PALETTES[index%len(PALETTES)]
    bg=Image.new("RGB",(w,h),bg_color)
    d=ImageDraw.Draw(bg)
    light=index in (2,5)
    muted="#5E747E" if light else "#A1B3C2"
    # Large compositions replace the small repeated information card.
    shade="#D5E1DE" if light else "#253954"
    d.ellipse((358,527,1298,1467),fill=shade)
    d.polygon([(0,1219),(842,687),(1080,871),(1080,1407),(0,1531)],fill=bg_color)
    d.text((72,87),"THE DAILY STORY",font=font(24,True),fill=ink)
    d.text((738,91),day,font=font(21),fill=muted)
    d.text((72,151),candidate.get("signal_label","IN THE NEWS"),font=font(21),fill=muted)
    # Captions are over a stable dark band on both light and dark scenes.
    d.rounded_rectangle((63,1380,915,1588),radius=30,fill="#101C30")
    source_labels=", ".join(dict.fromkeys(s["publisher"] for s in candidate["sources"]))
    source_lines=wrap_pixels(d,"Sources: "+source_labels,font(21),815)[:2]
    for j,line in enumerate(source_lines): d.text((73,1640+j*29),line,font=font(21),fill=muted)
    d.text((73,1726),"ILLUSTRATIVE GRAPHICS  /  AI VOICE",font=font(18,True),fill=muted)
    titles=Image.new("RGBA",(w,h))
    td=ImageDraw.Draw(titles)
    td.text((72,222),CHAPTERS[index],font=font(24,True),fill=accent)
    title=package["title"] if index==0 else scene["title"]
    size=84 if index==0 else 76
    face=font(size,True)
    lines=wrap_pixels(td,title,face,830)
    while len(lines)>3 and size>48:
        size-=4;face=font(size,True);lines=wrap_pixels(td,title,face,830)
    if len(lines)>3:
        raise WorkflowError("Headline does not fit the storyboard safely")
    for j,line in enumerate(lines): td.text((70,282+j*(size+17)),line,font=face,fill=ink)
    kind=story_visual_kind(scene,candidate)
    hero,prop=illustration(kind,accent,ink)
    if index==7:
        hero=Image.new("RGBA",(900,680));hd=ImageDraw.Draw(hero)
        hd.rounded_rectangle((107,80,769,536),radius=82,fill=ink)
        hd.polygon([(180,514),(209,654),(361,514)],fill=ink)
        hd.text((323,89),"?",font=font(352,True),fill=bg_color)
    return {"background":bg,"titles":titles,"hero":hero,"prop":prop,"accent":accent,
            "ink":ink,"index":index,"kind":kind,"cfg":cfg}


def ease(value):
    value=max(0,min(1,value))
    return 1-(1-value)**3


def scene_frame(assets, seconds, length):
    from PIL import ImageDraw
    image=assets["background"].copy()
    d=ImageDraw.Draw(image)
    index=assets["index"]
    intro=ease(seconds/.65)
    # Independent title, illustration and prop motion, rather than a static zoom.
    hero_x=46+round(18*math.sin(seconds*.48+index))
    hero_y=623+round(10*math.sin(seconds*.72)) + round((1-intro)*115)
    image.paste(assets["hero"],(hero_x,hero_y),assets["hero"])
    if index!=7:
        prop_x=728+round(19*math.sin(seconds*.68+index))
        prop_y=739+round(16*math.sin(seconds*.87+index))-round((1-ease(max(0,seconds-.25)/.7))*150)
        if assets["kind"]=="sport": prop_x=182+round(430*((seconds/length+.12)%1));prop_y=1002
        if assets["kind"]=="funding": prop_x=703;prop_y=1080+round(16*math.sin(seconds*1.2))
        image.paste(assets["prop"],(prop_x,prop_y),assets["prop"])
    # A subtle moving accent and section progress keep longer scenes alive.
    for i in range(4):
        x=87+i*239+round(12*math.sin(seconds*.55+i))
        y=612+round(17*math.cos(seconds*.61+i*2))
        d.ellipse((x,y,x+7,y+7),fill=assets["accent"])
    image.paste(assets["titles"],(0,round((1-intro)*-52)),assets["titles"])
    d.line((73,596,73+round(165*ease(seconds/.8)),596),fill=assets["accent"],width=6)
    count=8;gap=11;width=(824-gap*(count-1))/count
    for j in range(count):
        x=73+j*(width+gap)
        d.rounded_rectangle((x,1791,x+width,1797),radius=3,fill="#526071")
        filled=1 if j<index else min(1,seconds/max(length,.1)) if j==index else 0
        if filled: d.rounded_rectangle((x,1791,x+max(6,width*filled),1797),radius=3,fill=assets["accent"])
    return image


def card(scene, package, candidate, index, cfg, target, day):
    """A representative still from the same animation used in the real video."""
    assets=scene_assets(scene,package,candidate,index,cfg,day)
    scene_frame(assets,2,10).save(target)


def segment_duration(seconds, fps):
    # No per-scene minimum and no long reading pauses: length comes from the story.
    return math.ceil((seconds+.16)*fps)/fps


def animate_segment(scene, package, candidate, index, cfg, day, wav_path, output, length):
    assets=scene_assets(scene,package,candidate,index,cfg,day)
    command=["ffmpeg","-hide_banner","-loglevel","error","-y","-f","rawvideo","-pix_fmt","rgb24",
             "-s",str(cfg["width"])+"x"+str(cfg["height"]),"-r",str(cfg["fps"]),"-i","pipe:0","-i",str(wav_path),
             "-af","apad","-t",str(length),"-c:v","libx264","-preset","veryfast","-crf","23","-threads","2",
             "-pix_fmt","yuv420p","-c:a","aac","-b:a","128k","-ar","44100","-ac","1","-movflags","+faststart",str(output)]
    proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for n in range(round(length*cfg["fps"])):
            proc.stdin.write(scene_frame(assets,n/cfg["fps"],length).tobytes())
        proc.stdin.close()
        error=proc.stderr.read().decode(errors="replace")
        if proc.wait(): raise WorkflowError("Animation encoder failed: "+error[-1600:])
    except BrokenPipeError:
        proc.stdin.close()
        error=proc.stderr.read().decode(errors="replace");proc.wait()
        raise WorkflowError("Animation encoder stopped: "+error[-1600:]) from None
    except BaseException:
        if proc.poll() is None: proc.kill();proc.wait()
        raise


def render(package, candidate, out, cfg, day):
    from piper import PiperVoice, SynthesisConfig
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True)
    voice=PiperVoice.load(str(model_dir()/"en_US-ljspeech-high.onnx"))
    syn_config=SynthesisConfig(length_scale=1.02,volume=0.9)
    audio=[]
    for i,scene in enumerate(package["scenes"]):
        wav_path=out/f"scene-{i+1}.wav"
        with wave.open(str(wav_path),"wb") as stream: voice.synthesize_wav(scene["narration"],stream,syn_config=syn_config)
        seconds=duration(wav_path)
        if seconds<1: raise WorkflowError("Neural voice output was too short")
        audio.append((wav_path,seconds,segment_duration(seconds,cfg["fps"])))
    del voice
    speech_seconds=sum(a[1] for a in audio)
    elapsed=sum(a[2] for a in audio)
    if speech_seconds<cfg["min_seconds"]:
        raise WorkflowError(f"Narration is only {speech_seconds:.1f}s; expand the actual story instead of padding silence")
    if elapsed>cfg["max_seconds"]:
        raise WorkflowError(f"Narration duration {elapsed:.1f}s exceeds the configured maximum")
    cues,segments,cursor=[],[],0.0
    for i,(scene,(wav_path,seconds,length)) in enumerate(zip(package["scenes"],audio)):
        scene_cues=subtitles(scene["narration"],cursor,seconds)
        scene_cues[-1]["end"]=round(cursor+length,3);cues.extend(scene_cues)
        segment=out/f"segment-{i+1}.mp4"
        print(f"Animating story scene {i+1}/{len(audio)} ({length:.1f}s)",flush=True)
        animate_segment(scene,package,candidate,i,cfg,day,wav_path,segment,length)
        segments.append(segment);cursor+=length
    write_subtitles(cues,out,cfg)
    (out/"concat.txt").write_text("\n".join("file '"+p.name+"'" for p in segments)+"\n")
    output=out/"video.mp4"
    run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","concat","-safe","0","-i",str(out/"concat.txt"),
         "-vf","ass="+str(out/"subtitles.ass"),"-af","loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:v","libx264","-preset","veryfast","-crf","22","-threads","2","-pix_fmt","yuv420p",
         "-c:a","aac","-b:a","128k","-ar","44100","-movflags","+faststart",str(output)])
    report=probe(output,cfg)
    report.update({"subtitle_cues":len(cues),"narration_seconds":round(speech_seconds,2),"scene_count":len(audio),
                   "story_beats":[s.get("beat") for s in package["scenes"]],"visual_style":"Animated full-scene original illustrations",
                   "pause_seconds":round(elapsed-speech_seconds,2),
                   "subtitle_timing":"Exact scene boundaries, estimated word groups within scenes"})
    dump(out/"media-validation.json",report)
    return report


def probe(path,cfg):
    info=json.loads(run(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(path)]))
    video=next((s for s in info["streams"] if s["codec_type"]=="video"),None)
    audio=next((s for s in info["streams"] if s["codec_type"]=="audio"),None)
    if not video or not audio or video["codec_name"]!="h264" or audio["codec_name"]!="aac":
        raise WorkflowError("Final file must contain H.264 video and AAC audio")
    if (video["width"],video["height"])!=(cfg["width"],cfg["height"]) or video["pix_fmt"]!="yuv420p":
        raise WorkflowError("Final video has unexpected geometry or pixel format")
    seconds=float(info["format"]["duration"])
    if not cfg["min_seconds"] <= seconds <= cfg["max_seconds"]+0.2:
        raise WorkflowError("Final file duration is outside the configured range")
    if abs(float(video.get("duration",seconds))-float(audio.get("duration",seconds)))>0.4:
        raise WorkflowError("Final audio and video durations do not match")
    if Path(path).stat().st_size > 50_000_000:
        raise WorkflowError("Final video exceeded the 50 MB workflow cap")
    # Decode the whole file, rather than trusting only the container header.
    run(["ffmpeg","-hide_banner","-loglevel","error","-i",str(path),"-f","null","-"])
    return {"width":video["width"],"height":video["height"],"video_codec":video["codec_name"],
            "audio_codec":audio["codec_name"],"seconds":seconds,"bytes":Path(path).stat().st_size,"decoded_ok":True}

import json
import math
import re
import subprocess
import wave
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
Style: Default,DejaVu Sans,58,&H00FFFFFF,&H0000FFFF,&H00110B06,&H90110B06,-1,0,0,0,100,100,0,0,1,4,1,2,95,190,410,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for cue in cues:
        text = cue["text"].replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ")
        lines.append(f"Dialogue: 0,{timestamp(cue['start'],True)},{timestamp(cue['end'],True)},Default,,0,0,0,,{text}")
    (out / "subtitles.ass").write_text(header+"\n".join(lines)+"\n")


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
    words = " ".join((candidate.get("title", ""), scene.get("title", ""), scene.get("narration", ""))).lower()
    if any(x in words for x in ("funding", "grant", "budget", "money", "finance")): return "funding"
    if any(x in words for x in ("appoint", "administrator", "staff", "role", "official")): return "people"
    if any(x in words for x in ("settlement", "investigation", "civil rights", "agreement", "court", "law")): return "rights"
    if any(x in words for x in ("university", "school", "college", "campus", "student")): return "campus"
    if any(x in words for x in ("nasa", "space", "planet", "spacecraft", "science")): return "science"
    if any(x in words for x in ("football", "soccer", "rugby", "sport", "match")): return "sport"
    if any(x in words for x in ("phone", "technology", "tech", "artificial intelligence")): return "technology"
    return "spotlight"


def draw_story_icon(draw, kind, cx, cy, accent):
    ink, dark = "#dce9e8", "#17343d"
    if kind == "rights":
        draw.rounded_rectangle((cx-116,cy-132,cx+116,cy+132),radius=20,fill=dark,outline=accent,width=7)
        draw.polygon([(cx+48,cy-132),(cx+116,cy-64),(cx+48,cy-64)],fill="#244750")
        for y in (cy-38,cy-4,cy+30): draw.rounded_rectangle((cx-76,y,cx+61,y+10),radius=5,fill=ink)
        draw.ellipse((cx+22,cy+63,cx+102,cy+143),fill=accent)
        draw.line((cx+43,cy+100,cx+59,cy+116,cx+86,cy+82),fill=dark,width=9)
    elif kind == "campus":
        draw.polygon([(cx-190,cy-72),(cx,cy-190),(cx+190,cy-72)],fill=accent)
        draw.rectangle((cx-168,cy-68,cx+168,cy+151),fill=dark,outline=ink,width=5)
        draw.rectangle((cx-198,cy+145,cx+198,cy+177),fill=accent)
        for x in (cx-112,cx-38,cx+38,cx+112): draw.rounded_rectangle((x-19,cy-38,x+19,cy+123),radius=18,fill=ink)
        draw.rectangle((cx-36,cy+66,cx+36,cy+145),fill=accent)
    elif kind == "people":
        draw.ellipse((cx-51,cy-145,cx+51,cy-43),fill=accent)
        draw.rounded_rectangle((cx-98,cy-24,cx+98,cy+157),radius=70,fill=accent)
        draw.rounded_rectangle((cx+75,cy+22,cx+194,cy+151),radius=18,fill=dark,outline=ink,width=5)
        draw.line((cx+100,cy+63,cx+169,cy+63),fill=ink,width=7)
        draw.line((cx+100,cy+91,cx+158,cy+91),fill=ink,width=7)
    elif kind == "funding":
        for x,top,count in ((cx-168,cy+38,2),(cx-45,cy-8,3),(cx+78,cy-62,4)):
            for row in range(count):
                y=top-row*39; draw.ellipse((x,y,x+92,y+34),fill=accent if row==count-1 else "#538071",outline=ink,width=3)
        draw.line((cx-105,cy-118,cx-3,cy-156,cx+70,cy-112,cx+164,cy-188),fill=ink,width=11)
        draw.polygon([(cx+164,cy-188),(cx+125,cy-181),(cx+159,cy-145)],fill=ink)
    elif kind == "science":
        draw.ellipse((cx-173,cy-70,cx+173,cy+70),outline=accent,width=8)
        draw.ellipse((cx-70,cy-173,cx+70,cy+173),outline=ink,width=7)
        draw.ellipse((cx-140,cy-140,cx+140,cy+140),outline="#5c858b",width=7)
        draw.ellipse((cx-28,cy-28,cx+28,cy+28),fill=accent)
    elif kind == "sport":
        draw.ellipse((cx-160,cy-160,cx+160,cy+160),fill=dark,outline=accent,width=10)
        draw.polygon([(cx,cy-70),(cx+66,cy-23),(cx+41,cy+55),(cx-41,cy+55),(cx-66,cy-23)],outline=ink,fill="#244750")
        for dx,dy in ((-145,-75),(145,-75),(-90,135),(90,135)): draw.line((cx+dx,cy+dy,cx+dx//2,cy+dy//2),fill=ink,width=7)
    elif kind == "technology":
        draw.rounded_rectangle((cx-116,cy-186,cx+116,cy+186),radius=32,fill=dark,outline=accent,width=9)
        draw.rounded_rectangle((cx-78,cy-132,cx+78,cy+103),radius=12,fill="#254751")
        draw.ellipse((cx-12,cy+129,cx+12,cy+153),fill=ink)
        for y in (-94,-25,44): draw.line((cx-54,cy+y,cx+54,cy+y),fill=accent,width=7)
    else:
        draw.ellipse((cx-164,cy-164,cx+164,cy+164),fill=dark,outline=accent,width=8)
        pts=[]
        for i in range(16):
            r=150 if i%2==0 else 68; a=-math.pi/2+i*math.pi/8; pts.append((cx+math.cos(a)*r,cy+math.sin(a)*r))
        draw.polygon(pts,fill=accent); draw.ellipse((cx-26,cy-26,cx+26,cy+26),fill=dark)


def card(scene, package, candidate, index, cfg, target, day):
    from PIL import Image, ImageDraw
    w,h = cfg["width"],cfg["height"]
    image = Image.new("RGB", (w,h), "#071b22")
    draw = ImageDraw.Draw(image)
    for y in range(h):
        ratio=y/h
        draw.line([(0,y),(w,y)], fill=(int(6+9*ratio),int(24+13*ratio),int(33+13*ratio)))
    for x in range(60,w,90):
        for y in range(340,1300,90):
            draw.ellipse((x,y,x+3,y+3),fill="#27404a")
    accent = ["#83e8b2", "#ffbc72", "#8bd9ef", "#cfb5fb"][index]
    draw.rounded_rectangle((72,104,340,164),radius=30,fill="#173b3b")
    draw.text((95,117),"DAILY EXPLAINER",font=font(26,True),fill="#b4f2d3")
    draw.text((685,122),day,font=font(25),fill="#a0b3b8")
    draw.text((75,226),candidate["signal_label"],font=font(27,True),fill=accent)
    display_title=package["title"] if index==0 else scene["title"]
    titlefont = font(76, True)
    lines = wrap_pixels(draw,display_title,titlefont,810)
    if len(lines)>4:
        titlefont=font(62,True)
        lines=wrap_pixels(draw,display_title,titlefont,810)
    for j,line in enumerate(lines):
        draw.text((72,315+j*102),line,font=titlefont,fill="#f5faf6")
    # Topic-matched vector art keeps the original visuals distinct without stock footage.
    draw.rounded_rectangle((92,760,888,1225),radius=42,fill="#102a34",outline="#274b55",width=3)
    draw.text((132,792),f"0{index+1}  /  04",font=font(25,True),fill=accent)
    kind=story_visual_kind(scene,candidate)
    labels={"rights":"CIVIL RIGHTS","campus":"EDUCATION","people":"LEADERSHIP","funding":"FUNDING","science":"SCIENCE","sport":"SPORT","technology":"TECHNOLOGY","spotlight":"THE STORY"}
    label_font=font(20,True); label=labels[kind]
    draw.text((842-draw.textlength(label,font=label_font),797),label,font=label_font,fill="#a4bbc1")
    cx,cy=490,1002
    draw.ellipse((cx-204,cy-204,cx+204,cy+204),fill="#0b2028",outline="#1d3d47",width=3)
    draw_story_icon(draw,kind,cx,cy,accent)
    draw.text((132,1168),"THE STORY AT A GLANCE",font=font(22,True),fill="#93aeb5")
    draw.text((75,1284),"THE DAILY EXPLAINER",font=font(23,True),fill="#93aeb5")
    # Subtitle space is kept clear of TikTok's right-side controls and bottom caption.
    draw.rounded_rectangle((65,1360,915,1550),radius=24,fill="#102a34")
    source_labels = ", ".join(dict.fromkeys(s["publisher"] for s in candidate["sources"]))
    source_lines=wrap_pixels(draw,"Sources: " + source_labels,font(22),800)[:2]
    for j,line in enumerate(source_lines):
        draw.text((75,1600+j*29),line,font=font(22),fill="#a4bbc1")
    draw.text((75,1695),"AI voice  /  Original visuals",font=font(21),fill="#819da6")
    for i in range(4):
        draw.rounded_rectangle((75+i*208,1770,259+i*208,1777),radius=3,fill=accent if i<=index else "#314b54")
    image.save(target)


def render(package, candidate, out, cfg, day):
    from piper import PiperVoice, SynthesisConfig
    out=Path(out).resolve()
    out.mkdir(parents=True,exist_ok=True)
    voice=PiperVoice.load(str(model_dir()/"en_US-ljspeech-high.onnx"))
    cues,segments,elapsed=[],[],0.0
    syn_config=SynthesisConfig(length_scale=1.0,volume=0.9)
    for i,scene in enumerate(package["scenes"]):
        wav_path=out/f"scene-{i+1}.wav"
        with wave.open(str(wav_path),"wb") as stream:
            voice.synthesize_wav(scene["narration"],stream,syn_config=syn_config)
        seconds=duration(wav_path)
        if seconds < 1:
            raise WorkflowError("Neural voice output was too short")
        # Frame-quantize segment lengths so subtitle boundaries match the concatenated video.
        frames=math.ceil(max(seconds+0.18,cfg["min_seconds"]/4)*cfg["fps"])
        segment_seconds=frames/cfg["fps"]
        scene_cues=subtitles(scene["narration"],elapsed,seconds)
        scene_cues[-1]["end"]=round(elapsed+segment_seconds,3)
        cues.extend(scene_cues)
        card_path=out/f"scene-{i+1}.png"
        card(scene,package,candidate,i,cfg,card_path,day)
        segment=out/f"segment-{i+1}.mp4"
        zoom=f"zoompan=z='min(1.0+on*0.0002,1.06)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={cfg['width']}x{cfg['height']}:fps={cfg['fps']},format=yuv420p"
        run(["ffmpeg","-hide_banner","-loglevel","error","-y","-loop","1","-framerate",str(cfg["fps"]),"-i",str(card_path),"-i",str(wav_path),
             "-vf",zoom,"-af","apad","-t",str(segment_seconds),"-c:v","libx264","-preset","veryfast","-crf","23",
             "-threads","2","-c:a","aac","-b:a","128k","-ar","44100","-ac","1","-movflags","+faststart",str(segment)])
        segments.append(segment)
        elapsed+=segment_seconds
    if not cfg["min_seconds"] <= elapsed <= cfg["max_seconds"]:
        raise WorkflowError(f"Narration duration {elapsed:.1f}s is outside the configured range")
    write_subtitles(cues,out,cfg)
    (out/"concat.txt").write_text("\n".join("file '"+p.name+"'" for p in segments)+"\n")
    output=out/"video.mp4"
    # Fixed filenames avoid filter/concat injection from article titles or model text.
    run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","concat","-safe","0","-i",str(out/"concat.txt"),
         "-vf","ass="+str(out/"subtitles.ass"),"-af","loudnorm=I=-16:TP=-1.5:LRA=11",
         "-c:v","libx264","-preset","veryfast","-crf","22","-threads","2","-pix_fmt","yuv420p",
         "-c:a","aac","-b:a","128k","-ar","44100","-movflags","+faststart",str(output)])
    report=probe(output,cfg)
    report["subtitle_cues"]=len(cues)
    report["subtitle_timing"]="Exact scene boundaries, estimated word groups within scenes"
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

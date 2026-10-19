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
    draw.text((95,117),cfg["brand"],font=font(26,True),fill="#b4f2d3")
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
    # An original abstract orbit illustration: no stock footage or licensing dependency.
    cx,cy=450,1040
    for r in [80,135,190]:
        draw.ellipse((cx-r,cy-r,cx+r,cy+r),outline="#36545c",width=3)
    for i in range(28):
        angle=i*math.tau/28 + index*.22
        radius=140+(i%3)*24
        x=cx+math.cos(angle)*radius
        y=cy+math.sin(angle)*radius
        dot=5+(i%4)*3
        draw.ellipse((x-dot,y-dot,x+dot,y+dot),fill=accent if i%3==index%3 else "#41616a")
    draw.rounded_rectangle((cx-102,cy-77,cx+102,cy+77),radius=26,fill="#19343d",outline=accent,width=2)
    draw.text((cx-43,cy-58),f"0{index+1}",font=font(85,True),fill=accent)
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
        frames=math.ceil((seconds+0.18)*cfg["fps"])
        segment_seconds=frames/cfg["fps"]
        cues.extend(subtitles(scene["narration"],elapsed,seconds))
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

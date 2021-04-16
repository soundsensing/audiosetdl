
import sys
import os.path
import json

here = os.path.dirname(__file__)

sys.path.insert(0, os.path.join(here, '..'))
import download_audioset as audiosetdl



def read_json_file(path):
    with open(path, 'r') as f:
        contents = f.read()
        data = json.loads(contents)
        return data

def test_get_best_audio():

    p = os.path.join(here, 'data/69kudlOXwMs.info.json')
    info = read_json_file(p)
    expected_best_bitrate = 130.955

    s = audiosetdl.sort_audio_formats(info["formats"])    
    print(list(f['abr'] for f in s))

    assert s[0]['abr'] == expected_best_bitrate

    best = audiosetdl.get_best_audio_format(info["formats"])
    print(best['abr'])

def test_get_best_video():
    p = os.path.join(here, 'data/69kudlOXwMs.info.json')
    info = read_json_file(p)

    for f in info['formats']:
        print(f.get('width'), f.get('vbr'), f.get('tbr'))

    expected_best_width = 1280
    expected_best_bitrate_videoaudio = 1662.976
    expected_best_bitrate_video_only = 1530.31

    # with audio
    best = audiosetdl.get_best_video_format(info["formats"], video_mode='bestvideoaudio')
    assert best['width'] == expected_best_width
    assert best['tbr'] == expected_best_bitrate_videoaudio

    # without audio
    best = audiosetdl.get_best_video_format(info["formats"], video_mode='bestvideo')
    assert best['width'] == expected_best_width
    assert best['tbr'] == expected_best_bitrate_video_only


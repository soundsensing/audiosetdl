#!/usr/bin/env python
"""
Downloads Google's AudioSet dataset locally
"""
import argparse
import atexit
import collections
import csv
import logging.handlers
import multiprocessing as mp
import os
import random
import shutil
import sys
import traceback as tb
import urllib.request
from functools import partial

import multiprocessing_logging

from errors import SubprocessError, FfmpegValidationError, \
                   FfmpegIncorrectDurationError, FfmpegUnopenableFileError
from log import init_file_logger, init_console_logger
from utils import run_command, is_url, get_filename, \
    get_subset_name, get_media_filename, HTTP_ERR_PATTERN
from validation import validate_audio, validate_video

LOGGER = logging.getLogger('audiosetdl')
LOGGER.setLevel(logging.DEBUG)

EVAL_URL = 'http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/eval_segments.csv'
BALANCED_TRAIN_URL = 'http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/balanced_train_segments.csv'
UNBALANCED_TRAIN_URL = 'http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/unbalanced_train_segments.csv'


def parse_arguments():
    """
    Parse arguments from the command line


    Returns:
        args:  Argument dictionary
               (Type: dict[str, str])
    """
    parser = argparse.ArgumentParser(description='Download AudioSet data locally')

    parser.add_argument('-f',
                        '--ffmpeg',
                        dest='ffmpeg_path',
                        action='store',
                        type=str,
                        default='./bin/ffmpeg/ffmpeg',
                        help='Path to ffmpeg executable')

    parser.add_argument('-fp',
                        '--ffprobe',
                        dest='ffprobe_path',
                        action='store',
                        type=str,
                        default='./bin/ffmpeg/ffprobe',
                        help='Path to ffprobe executable')

    parser.add_argument('-e',
                        '--eval',
                        dest='eval_segments_path',
                        action='store',
                        type=str,
                        default=EVAL_URL,
                        help='Path to evaluation segments file')

    parser.add_argument('-b',
                        '--balanced-train',
                        dest='balanced_train_segments_path',
                        action='store',
                        type=str,
                        default=BALANCED_TRAIN_URL,
                        help='Path to balanced train segments file')

    parser.add_argument('-u',
                        '--unbalanced-train',
                        dest='unbalanced_train_segments_path',
                        action='store',
                        type=str,
                        default=UNBALANCED_TRAIN_URL,
                        help='Path to unbalanced train segments file')

    parser.add_argument('-ac',
                        '--audio-codec',
                        dest='audio_codec',
                        action='store',
                        type=str,
                        default='flac',
                        help='Name of audio codec used by ffmpeg to encode output audio')

    parser.add_argument('-asr',
                        '--audio-sample-rate',
                        dest='audio_sample_rate',
                        action='store',
                        type=int,
                        default=48000,
                        help='Target audio sample rate (in Hz)')

    parser.add_argument('-abd',
                        '--audio-bit-depth',
                        dest='audio_bit_depth',
                        action='store',
                        type=int,
                        default=16,
                        help='Target audio sample bit depth')

    parser.add_argument('-vc',
                        '--video-codec',
                        dest='video_codec',
                        action='store',
                        type=str,
                        default='h264',
                        help='Name of video codec used by ffmpeg to encode output audio')

    parser.add_argument('-af',
                        '--audio-format',
                        dest='audio_format',
                        action='store',
                        type=str,
                        default='flac',
                        help='Name of audio format used by ffmpeg for output audio')

    parser.add_argument('-vf',
                        '--video-format',
                        dest='video_format',
                        action='store',
                        type=str,
                        default='mp4',
                        help='Name of video format used by ffmpeg for output video')

    parser.add_argument('-vm',
                        '--video-mode',
                        dest='video_mode',
                        action='store',
                        type=str,
                        default='bestvideoaudio',
                        help="Name of the method in which video is downloaded. " \
                             "'bestvideo' obtains the best quality video that " \
                             "does not contain an audio stream. 'bestvideoaudio' " \
                             "obtains the best quality video that contains an " \
                             "audio stream. 'bestvideowithaudio' obtains the " \
                             "best quality video without an audio stream and " \
                             " merges it with audio stream")

    parser.add_argument('-vfr',
                        '--video-frame-rate',
                        dest='video_frame_rate',
                        action='store',
                        type=int,
                        default=30,
                        help='Target video frame rate (in fps)')

    parser.add_argument('-nr',
                        '--num-retries',
                        dest='num_retries',
                        action='store',
                        type=int,
                        default=10,
                        help='Number of retries when ffmpeg encounters an HTTP' \
                             'issue, which could be to unpredictable network behavior')
    parser.add_argument('-n',
                        '--num-workers',
                        dest='num_workers',
                        action='store',
                        type=int,
                        default=4,
                        help='Number of multiprocessing workers used to download videos')

    parser.add_argument('-nl',
                        '--no-logging',
                        dest='disable_logging',
                        action='store_true',
                        default=False,
                        help='Disables logging if flag enabled')

    parser.add_argument('-lp',
                        '--log-path',
                        dest='log_path',
                        action='store',
                        default=None,
                        help='Path to log file generated by this script. ' \
                             'By default, the path is "./audiosetdl.log".')

    parser.add_argument('-ft',
                        '--ffmpeg-timeout',
                        dest='ffmpeg_timeout',
                        action='store',
                        default=1*60,
                        help='Maximum time for ffmpeg call (in seconds)')

    parser.add_argument('-v',
                        '--verbose',
                        dest='verbose',
                        action='store_true',
                        default=False,
                        help='Prints verbose info to stdout')

    parser.add_argument('data_dir',
                        action='store',
                        type=str,
                        help='Path to directory where AudioSet data will be stored')


    return vars(parser.parse_args())


def ffmpeg(ffmpeg_path, input_path, output_path, input_args=None,
           output_args=None, log_level='error', num_retries=10, timeout=None,
           validation_callback=None, validation_args=None):
    """
    Transform an input file using `ffmpeg`

    Args:
        ffmpeg_path:  Path to ffmpeg executable
                      (Type: str)

        input_path:   Path/URL to input file(s)
                      (Type: str or iterable)

        output_path:  Path/URL to output file
                      (Type: str)

        input_args:   Options/flags for input files
                      (Type: list[str])

        output_args:  Options/flags for output files
                      (Type: list[str])

        log_level:    ffmpeg logging level
                      (Type: str)

        num_retries:  Number of retries if ffmpeg encounters an HTTP issue
                      (Type: int)
    """

    if type(input_path) == str:
        inputs = ['-i', input_path]
    elif isinstance(input_path, collections.Iterable):
        inputs = []
        for path in input_path:
            inputs.append('-i')
            inputs.append(path)
    else:
        error_msg = '"input_path" must be a str or an iterable, but got type {}'
        raise ValueError(error_msg.format(str(type(input_path))))

    if not input_args:
        input_args = []
    if not output_args:
        output_args = []

    last_err = None
    for attempt in range(num_retries):
        try:
            args = [ffmpeg_path] + input_args + inputs + output_args + [output_path, '-loglevel', log_level]
            run_command(args, timeout=timeout)

            # Validate if a callback was passed in
            if validation_callback is not None:
                validation_args = validation_args or {}
                validation_callback(output_path, **validation_args)
            break
        except SubprocessError as e:
            last_err = e
            stderr = e.cmd_stderr.rstrip()
            if stderr.endswith('already exists. Exiting.'):
                LOGGER.info('ffmpeg output file "{}" already exists.'.format(output_path))
                break
            elif HTTP_ERR_PATTERN.match(stderr):
                # Retry if we got a 4XX or 5XX, in case it was just a network issue
                continue

            LOGGER.error(str(e) + '. Retrying...')
            if os.path.exists(output_path):
                os.remove(output_path)

        except FfmpegIncorrectDurationError as e:
            last_err = e
            if attempt < num_retries - 1 and os.path.exists(output_path):
                os.remove(output_path)
            # If the duration of the output audio is different, alter the
            # duration argument to account for this difference and try again
            duration_diff = e.target_duration - e.actual_duration
            try:
                duration_idx = input_args.index('-t') + 1
                input_args[duration_idx] = str(float(input_args[duration_idx]) + duration_diff)
            except ValueError:
                duration_idx = output_args.index('-t') + 1
                output_args[duration_idx] = str(float(output_args[duration_idx]) + duration_diff)

            LOGGER.warning(str(e) +'; Retrying...')
            continue

        except FfmpegUnopenableFileError as e:
            last_err = e
            # Always remove unopenable files
            if os.path.exists(output_path):
                os.remove(output_path)
            # Retry if the output did not validate
            LOGGER.info('ffmpeg output file "{}" could not be opened: {}. Retrying...'.format(output_path, e.open_error))
            continue

        except FfmpegValidationError as e:
            last_err = e
            if attempt < num_retries - 1 and os.path.exists(output_path):
                os.remove(output_path)
            # Retry if the output did not validate
            LOGGER.info('ffmpeg output file "{}" did not validate: {}. Retrying...'.format(output_path, e))
            continue
    else:
        error_msg = 'Maximum number of retries ({}) reached. Could not obtain inputs at {}. Error: {}'
        LOGGER.error(error_msg.format(num_retries, input_path, str(last_err)))


def get_video_info(url):

    import youtube_dl

    ydl_opts = {
        #'format': 'bestaudio/best',
        #'verbose': True,
        #'cookies': 'cookies.txt',
        #'print-traffic': True,
        'youtube_include_dash_manifest': False,
    }
    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(url, download=False)

    if 'entries' in result and len(result["entries"]):
        # Can be a playlist or a list of videos
        video = result['entries'][0]
    else:
        # Just a video
        video = result

    return video

def format_is_audio_only(format):
    t = format['acodec'] != 'none' \
        and format['vcodec'] == 'none' # no video present, audio only
    return t

def format_is_video_only(format):
    t = format['acodec'] == 'none' \
        and format['vcodec'] != 'none'
    return t

def format_is_video_with_audio(format):
    t = format['acodec'] != 'none' \
        and format['vcodec'] != 'none'
    return t

def sort_audio_formats(formats, by='abr'):
    f = filter(format_is_audio_only, formats) 
    s = sorted(f, key=lambda f: f.get(by, 0), reverse=True)
    return s

def format_is_not_dash(format):
    return format['protocol'] != 'http_dash_segments'

def filter_formats(formats):
    return list(filter(format_is_not_dash, formats))

def get_best_audio_format(formats):
    formats = filter_formats(formats)
    s = sort_audio_formats(formats)
    if len(s):
        return s[0]
    else:
        return None


# note: Not all formats have vbr?
def sort_video_formats(formats, with_audio=True, by=('width', 'tbr')):
    pred = format_is_video_with_audio if with_audio else format_is_video_only
    f = filter(pred, formats)

    def get_key(f):
        key = tuple(f[k] for k in by)
        return key

    s = sorted(f, key=get_key, reverse=True)
    return s


def get_best_video_format(formats, video_mode, sort_by=('width', 'tbr')):
    formats = filter_formats(formats)
    video_noaudio_formats = sort_video_formats(formats, with_audio=False, by=sort_by)
    video_audio_formats = sort_video_formats(formats, with_audio=True, by=sort_by)

    if video_mode == '':
        return None

    if video_mode in ('bestvideo', 'bestvideowithaudio'):
        # If there isn't a video only option, go with best video with audio
        if len(video_noaudio_formats):
            return video_noaudio_formats[0]
        else:
            return video_audio_formats[0]
    elif video_mode in ('bestvideoaudio', 'bestvideoaudionoaudio'):
        return video_audio_formats[0]
    else:
        raise ValueError('Invalid video mode: {}'.format(video_mode))

def download_yt_video(ytid, ts_start, ts_end, output_dir, ffmpeg_path, ffprobe_path,
                      audio_codec='flac', audio_format='flac',
                      audio_sample_rate=48000, audio_bit_depth=16,
                      video_codec='h264', video_format='mp4',
                      video_mode='bestvideoaudio', video_frame_rate=30,
                      num_retries=10, ffmpeg_timeout=None):
    """
    Download a Youtube video (with the audio and video separated).

    The audio will be saved in <output_dir>/audio and the video will be saved in
    <output_dir>/video.

    The output filename is of the format:
        <YouTube ID>_<start time in ms>_<end time in ms>.<extension>

    Args:
        ytid:          Youtube ID string
                       (Type: str)

        ts_start:      Segment start time (in seconds)
                       (Type: float)

        ts_start:      Segment end time (in seconds)
                       (Type: float)

        output_dir:    Output directory where video will be saved
                       (Type: str)

        ffmpeg_path:   Path to ffmpeg executable
                       (Type: str)

        ffprobe_path:  Path to ffprobe executable
                       (Type: str)

    Keyword Args:
        audio_codec:        Name of audio codec used by ffmpeg to encode
                            output audio
                            (Type: str)

        audio_format:       Name of audio container format used for output audio
                            (Type: str)

        audio_sample_rate:  Target audio sample rate (in Hz)
                            (Type: int)

        audio_bit_depth:    Target audio sample bit depth
                            (Type: int)

        video_codec:        Name of video codec used by ffmpeg to encode
                            output video
                            (Type: str)

        video_format:       Name of video container format used for output video
                            (Type: str)

        video_mode:         Name of the method in which video is downloaded.
                            'bestvideo' obtains the best quality video that does not
                            contain an audio stream. 'bestvideoaudio' obtains the
                            best quality video that contains an audio stream.
                            'bestvideowithaudio' obtains the best quality video
                            without an audio stream and merges it with audio stream.
                            (Type: bool)

        video_frame_rate:   Target video frame rate (in fps)
                            (Type: int)

        num_retries:        Number of attempts to download and process an audio
                            or video file with ffmpeg
                            (Type: int)


    Returns:
        video_filepath:  Filepath to video file
                         (Type: str)

        audio_filepath:  Filepath to audio file
                         (Type: str)
    """
    # Compute some things from the segment boundaries
    duration = ts_end - ts_start

    # Make the output format and video URL
    # Output format is in the format:
    #   <YouTube ID>_<start time in ms>_<end time in ms>.<extension>
    media_filename = get_media_filename(ytid, ts_start, ts_end)
    video_filepath = os.path.join(output_dir, 'video', media_filename + '.' + video_format)
    audio_filepath = os.path.join(output_dir, 'audio', media_filename + '.' + audio_format)
    video_page_url = 'https://www.youtube.com/watch?v={}'.format(ytid)

    # Get the direct URLs to the videos with best audio and with best video (with audio)

    url = f'https://www.youtube.com/watch?v={ytid}'
    video = get_video_info(url)

    #video_url = video.get('url')
    #if not video_url:
    #    print(video)
    #    raise ValueError("No URL??")
    video_duration = video['duration']
    print(video['id'], video_duration)

    end_past_video_end = False
    if ts_end > video_duration:
        warn_msg = "End time for segment ({} - {}) of video {} extends past end of video (length {} sec)"
        LOGGER.warning(warn_msg.format(ts_start, ts_end, ytid, video_duration))
        duration = video_duration - ts_start
        ts_end = ts_start + duration
        end_past_video_end = True

    best_video = get_best_video_format(video['formats'], video_mode=video_mode)
    best_audio = get_best_audio_format(video['formats'])
    best_video_url = best_video['url'] if best_video else None  
    best_audio_url = best_audio['url'] if best_audio else best_video['url']

    audio_info = {
        'sample_rate': audio_sample_rate,
        'channels': 2,
        #'bitrate': audio_bit_depth, # in recent sox this is not the bitdepth for FLAC
        'encoding': audio_codec.upper(),
        'duration': duration
    }
    video_info = {
        "r_frame_rate": "{}/1".format(video_frame_rate),
        "avg_frame_rate": "{}/1".format(video_frame_rate),
        'codec_name': video_codec.lower(),
        'duration': duration
    }
    # Download the audio
    audio_input_args = ['-n', '-ss', str(ts_start)]
    audio_output_args = ['-t', str(duration),
                         '-ar', str(audio_sample_rate),
                         '-vn',
                         '-ac', str(audio_info['channels']),
                         '-sample_fmt', 's{}'.format(audio_bit_depth),
                         '-f', audio_format,
                         '-acodec', audio_codec]
    ffmpeg(ffmpeg_path, best_audio_url, audio_filepath,
           input_args=audio_input_args, output_args=audio_output_args,
           num_retries=num_retries, validation_callback=validate_audio,
           validation_args={'audio_info': audio_info,
                            'end_past_video_end': end_past_video_end},
           timeout=ffmpeg_timeout)

    if not video_mode:
        pass

    elif video_mode != 'bestvideowithaudio':
        # Download the video
        video_input_args = ['-n', '-ss', str(ts_start)]
        video_output_args = ['-t', str(duration),
                             '-f', video_format,
                             '-r', str(video_frame_rate),
                             '-vcodec', video_codec]
        # Suppress audio stream if we don't want to audio in the video
        if video_mode in ('bestvideo', 'bestvideoaudionoaudio'):
            video_output_args.append('-an')

        ffmpeg(ffmpeg_path, best_video_url, video_filepath,
               input_args=video_input_args, output_args=video_output_args,
               num_retries=num_retries, validation_callback=validate_video,
               validation_args={'ffprobe_path': ffprobe_path,
                                'video_info': video_info,
                                'end_past_video_end': end_past_video_end},
               timeout=ffmpeg_timeout)
    else:
        # Download the best quality video, in lossless encoding
        if video_codec != 'h264':
            error_msg = 'Not currently supporting merging of best quality video with video for codec: {}'
            raise NotImplementedError(error_msg.format(video_codec))
        video_input_args = ['-n', '-ss', str(ts_start)]
        video_output_args = ['-t', str(duration),
                             '-f', video_format,
                             '-crf', '0',
                             '-preset', 'medium',
                             '-r', str(video_frame_rate),
                             '-an',
                             '-vcodec', video_codec]

        ffmpeg(ffmpeg_path, best_video_url, video_filepath,
               input_args=video_input_args, output_args=video_output_args,
               num_retries=num_retries, timeout=ffmpeg_timeout)

        # Merge the best lossless video with the lossless audio, and compress
        merge_video_filepath = os.path.splitext(video_filepath)[0] \
                               + '_merge.' + video_format
        video_input_args = ['-n']
        video_output_args = ['-f', video_format,
                             '-r', str(video_frame_rate),
                             '-vcodec', video_codec,
                             '-acodec', 'aac',
                             '-ar', str(audio_sample_rate),
                             '-ac', str(audio_info['channels']),
                             '-strict', 'experimental']

        ffmpeg(ffmpeg_path, [video_filepath, audio_filepath], merge_video_filepath,
               input_args=video_input_args, output_args=video_output_args,
               num_retries=num_retries, validation_callback=validate_video,
               validation_args={'ffprobe_path': ffprobe_path,
                                'video_info': video_info,
                                'end_past_video_end': end_past_video_end},
               timeout=ffmpeg_timeout,)

        # Remove the original video file and replace with the merged version
        if os.path.exists(merge_video_filepath):
            os.remove(video_filepath)
            shutil.move(merge_video_filepath, video_filepath)
        else:
            error_msg = 'Cannot find merged video for {} ({} - {}) at {}'
            LOGGER.error(error_msg.format(ytid, ts_start, ts_end, merge_video_filepath))

    LOGGER.info('Downloaded video {} ({} - {})'.format(ytid, ts_start, ts_end))

    return video_filepath, audio_filepath


def segment_mp_worker(ytid, ts_start, ts_end, data_dir, ffmpeg_path,
                      ffprobe_path, **ffmpeg_cfg):
    """
    Pool worker that downloads video segments.o

    Wraps around the download_yt_video function to catch errors and log them.

    Args:

        ytid:          Youtube ID string
                       (Type: str)

        ts_start:      Segment start time (in seconds)
                       (Type: float)

        ts_end:        Segment end time (in seconds)
                       (Type: float)

        data_dir:      Directory where videos will be saved
                       (Type: str)

        ffmpeg_path:   Path to ffmpeg executable
                       (Type: str)

        ffprobe_path:  Path to ffprobe executable
                       (Type: str)

    Keyword Args:
        **ffmpeg_cfg:  Configuration for audio and video
                       downloading and decoding done by ffmpeg
                       (Type: dict[str, *])
    """
    LOGGER.info('Attempting to download video {} ({} - {})'.format(ytid, ts_start, ts_end))

    # Download the video
    try:
        download_yt_video(ytid, ts_start, ts_end, data_dir, ffmpeg_path,
                          ffprobe_path, **ffmpeg_cfg)
    except SubprocessError as e:
        err_msg = 'Error while downloading video {}: {}; {}'.format(ytid, e, tb.format_exc())
        LOGGER.error(err_msg)
        return False

    except Exception as e:
        err_msg = 'Error while processing video {}: {}; {}'.format(ytid, e, tb.format_exc())
        LOGGER.error(err_msg)
        return False

    return True

def init_subset_data_dir(dataset_dir, subset_name):
    """
    Creates the data directories for the given subset

    Args:
        dataset_dir:  Path to dataset directory
                      (Type: str)

        subset_name:  Name of subset
                      (Type: str)

    Returns:
        data_dir:  Path to subset data dir
                   (Type: str)
    """
    # Derive audio and video directory names for this subset
    data_dir = os.path.join(dataset_dir, 'data', subset_name)
    audio_dir = os.path.join(data_dir, 'audio')
    video_dir = os.path.join(data_dir, 'video')
    os.makedirs(audio_dir, exist_ok=True)
    os.makedirs(video_dir, exist_ok=True)

    return data_dir


def download_subset_file(subset_url, dataset_dir):
    """
    Download a subset segments file from the given url to the given directory.

    Args:
        subset_url:   URL to subset segments file
                      (Type: str)

        dataset_dir:  Dataset directory where subset segment file will be stored
                      (Type: str)

    Returns:
        subset_path:  Path to subset segments file
                      (Type: str)

    """
    # Get filename of the subset file
    subset_filename = get_filename(subset_url)
    subset_name = get_subset_name(subset_url)
    subset_path = os.path.join(dataset_dir, subset_filename)

    os.makedirs(dataset_dir, exist_ok=True)

    # Open subset file as a CSV
    if not os.path.exists(subset_path):
        LOGGER.info('Downloading subset file for "{}"'.format(subset_name))
        with open(subset_path, 'w') as f:
            subset_data = urllib.request.urlopen(subset_url).read().decode()
            f.write(subset_data)

    return subset_path


def load_failures(path="failures.csv"):
    import os.path
    import pandas

    if not os.path.exists(path):
        return set()

    df = pandas.read_csv(path, quotechar="'", names=['youtube_id', 'error'])
    return set(df.youtube_id)


def check_output_exists(data_dir, ytid, ts_start, ts_end, audio_only=False, video_format='mp4', audio_format='flac'):

    # Skip files that already have been downloaded
    media_filename = get_media_filename(ytid, ts_start, ts_end)
    video_filepath = os.path.join(data_dir, 'video', media_filename + '.' + video_format)
    audio_filepath = os.path.join(data_dir, 'audio', media_filename + '.' + audio_format)

    output_exists = False
    audio_exists = os.path.exists(audio_filepath)
    if audio_only:
        output_exists = audio_exists
    else:
        output_exists = audio_exists and os.path.exists(video_filepath)

    return output_exists

def process_job(ytid, ts_start, ts_end, data_dir,
        ffmpeg_path, ffprobe_path, ffmpeg_cfg, failed_ids):

    audio_only = not bool(ffmpeg_cfg.get('video_mode'))
    output_exists = check_output_exists(data_dir, ytid, ts_start, ts_end, audio_only=audio_only)
    if output_exists:
        info_msg = 'Already downloaded video {} ({} - {}). Skipping.'
        LOGGER.info(info_msg.format(ytid, ts_start, ts_end))
        return

    # Skip files that have failed before
    if ytid in failed_ids:
        info_msg = 'Video failed previously {} ({} - {}). Skipping.'
        LOGGER.info(info_msg.format(ytid, ts_start, ts_end))
        return

    # FIXME: bring back incremental tracking of failures

    #with open("failures.csv", "a") as failures:   
    #    def log_failure(ytid, msg):
    #        m = msg.replace('\n', '\\n').replace('\r' ,'\\r')
    #        failures.write(f"{ytid},'{m}'\n")

    success = segment_mp_worker(ytid, ts_start, ts_end, data_dir, ffmpeg_path,
                      ffprobe_path, **ffmpeg_cfg)

    if success:
        return ytid
    else:
        return None


def download_subset_videos(subset_path, data_dir, ffmpeg_path, ffprobe_path,
                           num_workers, **ffmpeg_cfg):
    """
    Download subset segment file and videos

    Args:
        subset_path:   Path to subset segments file
                       (Type: str)

        data_dir:      Directory where dataset files will be saved
                       (Type: str)

        ffmpeg_path:   Path to ffmpeg executable
                       (Type: str)

        ffprobe_path:  Path to ffprobe executable
                       (Type: str)

        num_workers:   Number of multiprocessing workers used to download videos
                       (Type: int)

    Keyword Args:
        **ffmpeg_cfg:  Configuration for audio and video
                       downloading and decoding done by ffmpeg
                       (Type: dict[str, *])
    """
    subset_name = get_subset_name(subset_path)

    failed_ids = load_failures()
    LOGGER.info('Loaded failures, {}'.format(len(failed_ids)))

    LOGGER.info('Preparing jobs for subset "{}"'.format(subset_name))

    import joblib


    def setup_jobs(data):
        jobs = []

        remaining = []

        try:
            for row_idx, row in enumerate(data):
                # Skip commented lines
                if row[0][0] == '#':
                    continue
                ytid, ts_start, ts_end = row[0], float(row[1]), float(row[2])

                audio_only = not bool(ffmpeg_cfg.get('video_mode'))
                output_exists = check_output_exists(data_dir, ytid, ts_start, ts_end, audio_only=audio_only)
                if output_exists:
                    continue

                if ytid in failed_ids:
                    continue

                worker_args = [ytid, ts_start, ts_end,
                    data_dir, ffmpeg_path, ffprobe_path, ffmpeg_cfg, failed_ids]
            
                remaining.append((ytid, ts_start, ts_end))
                job = joblib.delayed(process_job)(*worker_args)
                jobs += [ job ]
        except csv.Error as e:
            LOGGER.error(f'CSV error in {subset_path} at line {row_idx}: {e}')

        df = pandas.DataFrame.from_records(remaining)
        df.to_csv("remaining.csv", index=False, header=False)

        return jobs

    # Prepare jobs
    jobs = []
    with open(subset_path, 'r') as f:
        subset_data = csv.reader(f)
        jobs = setup_jobs(subset_data)

    LOGGER.info('Starting {} download jobs for subset "{}"'.format(len(jobs), subset_name))

    # Execute jobs
    #print(len(jobs), jobs[0])
    results = joblib.Parallel(n_jobs=num_workers)(jobs)

    LOGGER.info('Finished download jobs for subset "{}"'.format(subset_name))



def download_subset(subset_path, dataset_dir, ffmpeg_path, ffprobe_path,
                    num_workers, **ffmpeg_cfg):
    """
    Download all files for a subset, including the segment file, and the audio and video files.

    Args:
        subset_path:    Path to subset segments file
                        (Type: str)

        dataset_dir:    Path to dataset directory where files are saved
                        (Type: str)

        ffmpeg_path:    Path to ffmpeg executable
                        (Type: str)

        ffprobe_path:   Path to ffprobe executable
                        (Type: str)

        num_workers:    Number of workers to download and process videos
                        (Type: int)

    Keyword Args:
        **ffmpeg_cfg:                   Configuration for audio and video
                                        downloading and decoding done by ffmpeg
                                        (Type: dict[str, *])

    Returns:

    """
    if is_url(subset_path):
        subset_path = download_subset_file(subset_path, dataset_dir)

    subset_name = get_subset_name(subset_path)
    data_dir = init_subset_data_dir(dataset_dir, subset_name)

    download_subset_videos(subset_path, data_dir, ffmpeg_path, ffprobe_path,
                           num_workers, **ffmpeg_cfg)


def download_audioset(data_dir, ffmpeg_path, ffprobe_path, eval_segments_path,
                      balanced_train_segments_path, unbalanced_train_segments_path,
                      disable_logging=False, verbose=False, num_workers=4,
                      log_path=None, **ffmpeg_cfg):
    """
    Download AudioSet files

    Args:
        data_dir:                       Directory where dataset files will
                                        be saved
                                        (Type: str)

        ffmpeg_path:                    Path to ffmpeg executable
                                        (Type: str)

        ffprobe_path:                   Path to ffprobe executable
                                        (Type: str)

        eval_segments_path:             Path to evaluation segments file
                                        (Type: str)

        balanced_train_segments_path:   Path to balanced train segments file
                                        (Type: str)

        unbalanced_train_segments_path: Path to unbalanced train segments file
                                        (Type: str)

    Keyword Args:
        disable_logging:                Disables logging to a file if True
                                        (Type: bool)

        verbose:                        Prints verbose information to stdout
                                        if True
                                        (Type: bool)

        num_workers:                    Number of multiprocessing workers used
                                        to download videos
                                        (Type: int)

        log_path:                       Path where log file will be saved. If
                                        None, saved to './audiosetdl.log'
                                        (Type: str or None)

        **ffmpeg_cfg:                   Configuration for audio and video
                                        downloading and decoding done by ffmpeg
                                        (Type: dict[str, *])
    """
    init_console_logger(LOGGER, verbose=verbose)
    if not disable_logging:
        init_file_logger(LOGGER, log_path=log_path)
    multiprocessing_logging.install_mp_handler()
    LOGGER.debug('Initialized logging.')

    download_subset(eval_segments_path, data_dir, ffmpeg_path, ffprobe_path,
                    num_workers, **ffmpeg_cfg)
    download_subset(balanced_train_segments_path, data_dir, ffmpeg_path, ffprobe_path,
                    num_workers, **ffmpeg_cfg)
    download_subset(unbalanced_train_segments_path, data_dir, ffmpeg_path, ffprobe_path,
                    num_workers, **ffmpeg_cfg)


if __name__ == '__main__':
    # TODO: Handle killing of ffmpeg (https://stackoverflow.com/questions/6488275/terminal-text-becomes-invisible-after-terminating-subprocess)
    #       so we don't have to use this hack
    atexit.register(lambda: os.system('stty sane') if sys.stdin.isatty() else None)
    download_audioset(**parse_arguments())

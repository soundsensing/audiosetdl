
from download_audioset import download_subset_videos 

def download(csv_path, data_dir, num_workers=10):

    ffmpeg_path = 'ffmpeg'
    ffprobe_path = 'ffprobe'

    ffmpeg_cfg = {
        'audio_codec': 'flac',
        'audio_sample_rate': 48000,
        'audio_bit_depth': 16,
        'audio_format': 'flac',
        'video_mode': '',
        'video_format': 'mp4',
        'video_codec': 'h264',
        'video_frame_rate': 30,
        'num_retries': 3,
        'ffmpeg_timeout': 60,
    }

    download_subset_videos(csv_path, data_dir, ffmpeg_path, ffprobe_path,
                           num_workers, **ffmpeg_cfg)


def parse():
    import argparse
    parser = argparse.ArgumentParser(description='Index directory of audio files')

    parser.add_argument('--segments', metavar='PATH', type=str, default=None,
                        help='CSV with video segments to download')
 
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Directory to place the downloaded data')

    parser.add_argument('--jobs', type=int, default=5,
                        help='Number of jobs to execute concurrently')


    args = parser.parse_args()

    return args

def main():

    args = parse()
    download(csv_path=args.segments, data_dir=args.output_dir, num_workers=args.jobs)

if __name__ == '__main__':
    main()

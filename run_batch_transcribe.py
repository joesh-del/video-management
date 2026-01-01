#!/usr/bin/env python3
"""Batch transcribe all videos that don't have transcripts."""

import os
import sys
import ssl
import tempfile
import subprocess
from pathlib import Path

# Fix SSL for Whisper model download
ssl._create_default_https_context = ssl._create_unverified_context

sys.path.insert(0, '/Users/josephs./Desktop/video-management')

from scripts.db import DatabaseSession, Video, Transcript, TranscriptSegment
from scripts.config_loader import get_config
import whisper
import boto3

# Load Whisper model once
print("Loading Whisper model...", flush=True)
model = whisper.load_model("base")
print("Model loaded!", flush=True)

config = get_config()
s3 = boto3.client(
    's3',
    aws_access_key_id=config.aws_access_key,
    aws_secret_access_key=config.aws_secret_key,
    region_name=config.aws_region,
)

# Google Drive base paths to check for original files
GDRIVE_PATHS = [
    Path("/Users/josephs./Library/CloudStorage/GoogleDrive-digital@danielsgoldin.com/Shared drives/5. Marketing/4. Videos"),
    Path("/Users/josephs./Desktop/video/base"),
]

def find_local_file(filename: str) -> Path:
    """Try to find the original file locally."""
    for base_path in GDRIVE_PATHS:
        if base_path.exists():
            # Search recursively
            for f in base_path.rglob(filename):
                if f.exists():
                    return f
    return None

def download_from_s3(s3_key: str, local_path: Path) -> bool:
    """Download a file from S3."""
    try:
        s3.download_file(config.s3_bucket, s3_key, str(local_path))
        return True
    except Exception as e:
        print(f"  S3 download failed: {e}", flush=True)
        return False

def transcribe_video(video_path: Path) -> dict:
    """Transcribe a video using Whisper."""
    result = model.transcribe(str(video_path), verbose=False)
    return result

def get_videos_to_transcribe():
    """Get list of videos that need transcription."""
    with DatabaseSession() as session:
        # Get all video IDs that already have completed transcripts
        transcribed_ids = set(
            t.video_id for t in session.query(Transcript).filter(
                Transcript.status == 'completed'
            ).all()
        )

        # Get videos that don't have transcripts
        videos = []
        for v in session.query(Video).all():
            if v.id not in transcribed_ids:
                videos.append({
                    'id': str(v.id),
                    'filename': v.filename,
                    's3_key': v.s3_key,
                })
        return videos

def main():
    videos = get_videos_to_transcribe()
    print(f"Found {len(videos)} videos to transcribe", flush=True)

    transcribed = 0
    failed = 0

    for i, video in enumerate(videos):
        print(f"\n[{i+1}/{len(videos)}] {video['filename'][:60]}...", flush=True)

        try:
            # Try to find local file first
            local_path = find_local_file(video['filename'])
            temp_file = None

            if local_path:
                print(f"  Using local file", flush=True)
                video_path = local_path
            else:
                # Download from S3
                print(f"  Downloading from S3...", flush=True)
                temp_file = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
                temp_file.close()
                video_path = Path(temp_file.name)

                if not download_from_s3(video['s3_key'], video_path):
                    print(f"  FAILED: Could not get video file", flush=True)
                    failed += 1
                    continue

            # Transcribe
            print(f"  Transcribing...", flush=True)
            result = transcribe_video(video_path)

            # Save to database
            with DatabaseSession() as session:
                from uuid import UUID
                transcript = Transcript(
                    video_id=UUID(video['id']),
                    s3_key=f"transcripts/{video['id']}.json",
                    status='completed',
                    language=result.get('language', 'en'),
                )
                session.add(transcript)
                session.flush()

                # Add segments
                for idx, seg in enumerate(result.get('segments', [])):
                    segment = TranscriptSegment(
                        transcript_id=transcript.id,
                        segment_index=idx,
                        start_time=seg['start'],
                        end_time=seg['end'],
                        text=seg['text'].strip(),
                        confidence=seg.get('avg_logprob', 0),
                    )
                    session.add(segment)

                session.commit()

            print(f"  OK - {len(result.get('segments', []))} segments", flush=True)
            transcribed += 1

            # Cleanup temp file
            if temp_file and Path(temp_file.name).exists():
                os.unlink(temp_file.name)

        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
            failed += 1

    print(f"\n{'='*60}", flush=True)
    print(f"Done: {transcribed} transcribed, {failed} failed", flush=True)
    print(f"{'='*60}", flush=True)

if __name__ == '__main__':
    main()

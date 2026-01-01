"""Batch upload videos from a directory to S3."""

import os
import re
import hashlib
from pathlib import Path
from datetime import datetime
import subprocess

import boto3
from tqdm import tqdm

from .config_loader import get_config
from .db import DatabaseSession, Video


def extract_metadata_from_path(file_path: Path) -> dict:
    """Extract metadata from folder/file naming conventions."""
    path_str = str(file_path)
    metadata = {
        'speaker': 'Dan Goldin',  # Default for this archive
        'event_name': None,
        'event_date': None,
        'description': None,
    }

    # Try to extract date from folder name (YYYYMMDD format)
    date_match = re.search(r'(\d{8})', path_str)
    if date_match:
        try:
            date_str = date_match.group(1)
            metadata['event_date'] = datetime.strptime(date_str, '%Y%m%d').date()
        except ValueError:
            pass

    # Try YYYYMM format
    if not metadata['event_date']:
        date_match = re.search(r'/(\d{6}) -', path_str)
        if date_match:
            try:
                date_str = date_match.group(1)
                metadata['event_date'] = datetime.strptime(date_str + '01', '%Y%m%d').date()
            except ValueError:
                pass

    # Extract event name from folder structure
    # Pattern: "YYYYMMDD - Event Name" or "Event Name"
    parts = file_path.parts
    for part in parts:
        if ' - ' in part:
            # Format: "YYYYMMDD - Event Name"
            event_part = part.split(' - ', 1)
            if len(event_part) > 1:
                metadata['event_name'] = event_part[1].strip()
                break

    # Determine era for description
    if metadata['event_date']:
        year = metadata['event_date'].year
        if 1992 <= year <= 2001:
            metadata['description'] = f"From Dan Goldin's tenure as NASA Administrator ({year})"
        elif year > 2001:
            metadata['description'] = f"Post-NASA speaking engagement ({year})"

    return metadata


def get_video_duration(file_path: Path) -> float:
    """Get video duration using ffprobe."""
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(file_path)
        ], capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except:
        return 0


def batch_upload(source_dir: str, dry_run: bool = False):
    """Upload all videos from source directory to S3."""
    config = get_config()
    source_path = Path(source_dir)

    # Find all video files
    video_extensions = {'.mp4', '.mov', '.MP4', '.MOV'}
    video_files = []
    for ext in video_extensions:
        video_files.extend(source_path.rglob(f'*{ext}'))

    print(f"Found {len(video_files)} videos to upload")

    if dry_run:
        for f in video_files[:10]:
            print(f"  Would upload: {f.name}")
        print(f"  ... and {len(video_files) - 10} more")
        return

    # Initialize S3
    s3 = boto3.client(
        's3',
        aws_access_key_id=config.aws_access_key,
        aws_secret_access_key=config.aws_secret_key,
        region_name=config.aws_region,
    )
    bucket = config.s3_bucket

    uploaded = 0
    skipped = 0
    failed = 0

    for video_path in tqdm(video_files, desc="Uploading"):
        try:
            # Generate unique S3 key
            file_hash = hashlib.md5(str(video_path).encode()).hexdigest()[:8]
            safe_name = re.sub(r'[^\w\-_\.]', '_', video_path.name)
            s3_key = f"videos/{safe_name.rsplit('.', 1)[0]}_{file_hash}.{video_path.suffix.lower().lstrip('.')}"

            # Check if already exists in database
            with DatabaseSession() as session:
                existing = session.query(Video).filter(
                    Video.original_filename == video_path.name
                ).first()
                if existing:
                    skipped += 1
                    continue

            # Get file info
            file_size = video_path.stat().st_size
            duration = get_video_duration(video_path)
            metadata = extract_metadata_from_path(video_path)

            # Upload to S3
            s3.upload_file(
                str(video_path),
                bucket,
                s3_key,
                ExtraArgs={'ContentType': 'video/mp4'},
                Callback=None
            )

            # Register in database
            with DatabaseSession() as session:
                video = Video(
                    filename=video_path.name,
                    original_filename=video_path.name,
                    s3_key=s3_key,
                    s3_bucket=bucket,
                    file_size_bytes=file_size,
                    duration_seconds=duration if duration > 0 else None,
                    format=video_path.suffix.lower().lstrip('.'),
                    status='uploaded',
                    speaker=metadata['speaker'],
                    event_name=metadata['event_name'],
                    event_date=metadata['event_date'],
                    description=metadata['description'],
                )
                session.add(video)
                session.commit()

            uploaded += 1

        except Exception as e:
            print(f"\nFailed: {video_path.name}: {e}")
            failed += 1

    print(f"\nComplete: {uploaded} uploaded, {skipped} skipped, {failed} failed")


if __name__ == '__main__':
    import sys
    source = sys.argv[1] if len(sys.argv) > 1 else None
    if source:
        batch_upload(source)
    else:
        print("Usage: python -m scripts.batch_upload <source_directory>")

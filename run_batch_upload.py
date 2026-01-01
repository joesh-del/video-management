#!/usr/bin/env python3
"""Simple batch upload script."""

import boto3
import re
import hashlib
from pathlib import Path
from datetime import datetime
import sys

sys.path.insert(0, '/Users/josephs./Desktop/video-management')

from scripts.config_loader import get_config
from scripts.db import DatabaseSession, Video

config = get_config()
s3 = boto3.client(
    's3',
    aws_access_key_id=config.aws_access_key,
    aws_secret_access_key=config.aws_secret_key,
    region_name=config.aws_region,
)
bucket = config.s3_bucket

source_dir = Path("/Users/josephs./Library/CloudStorage/GoogleDrive-digital@danielsgoldin.com/Shared drives/5. Marketing/4. Videos")

# Find all videos
videos = list(source_dir.rglob("*.mp4")) + list(source_dir.rglob("*.MP4")) + list(source_dir.rglob("*.mov")) + list(source_dir.rglob("*.MOV"))
print(f"Found {len(videos)} videos", flush=True)

uploaded = 0
skipped = 0
failed = 0

for i, video_path in enumerate(videos):
    try:
        # Check if already in DB
        with DatabaseSession() as session:
            existing = session.query(Video).filter(Video.original_filename == video_path.name).first()
            if existing:
                skipped += 1
                continue

        # Generate S3 key
        file_hash = hashlib.md5(str(video_path).encode()).hexdigest()[:8]
        safe_name = re.sub(r'[^\w\-_\.]', '_', video_path.name)
        ext = video_path.suffix.lower().lstrip('.')
        s3_key = f"videos/{safe_name.rsplit('.', 1)[0]}_{file_hash}.{ext}"

        # Extract metadata from path
        path_str = str(video_path)
        event_date = None
        event_name = None

        # Try YYYYMMDD
        date_match = re.search(r'/(\d{8}) -', path_str)
        if date_match:
            try:
                event_date = datetime.strptime(date_match.group(1), '%Y%m%d').date()
            except:
                pass

        # Try YYYYMM
        if not event_date:
            date_match = re.search(r'/(\d{6}) -', path_str)
            if date_match:
                try:
                    event_date = datetime.strptime(date_match.group(1) + '01', '%Y%m%d').date()
                except:
                    pass

        # Event name
        for part in video_path.parts:
            if ' - ' in part:
                parts = part.split(' - ', 1)
                if len(parts) > 1:
                    event_name = parts[1].strip()
                    break

        size_mb = video_path.stat().st_size / 1024 / 1024
        print(f"[{i+1}/{len(videos)}] {video_path.name[:50]}... ({size_mb:.1f}MB)", flush=True)

        # Upload
        s3.upload_file(str(video_path), bucket, s3_key)

        # Register in DB
        with DatabaseSession() as session:
            video = Video(
                filename=video_path.name,
                original_filename=video_path.name,
                s3_key=s3_key,
                s3_bucket=bucket,
                file_size_bytes=video_path.stat().st_size,
                format=ext,
                status='uploaded',
                speaker='Dan Goldin',
                event_name=event_name,
                event_date=event_date,
            )
            session.add(video)
            session.commit()

        uploaded += 1
        print(f"  OK", flush=True)

    except Exception as e:
        print(f"  FAILED: {e}", flush=True)
        failed += 1

print(f"\nDone: {uploaded} uploaded, {skipped} skipped, {failed} failed", flush=True)

"""Import Otter AI exports (docx + mp3 pairs) into the database."""

import os
import re
import uuid
from datetime import datetime, date
from pathlib import Path
from decimal import Decimal

import boto3
from docx import Document as DocxDocument

from db import get_session, AudioRecording, AudioSegment, Persona


def parse_otter_docx(docx_path: str) -> dict:
    """Parse an Otter AI docx file and extract metadata + segments.

    Otter format:
    - Line 1: Title
    - Line 2: Date and duration (e.g., "Fri, Dec 05, 2025 10:47AM • 21:01")
    - Lines 3-4: SUMMARY KEYWORDS / keyword list
    - Lines 5-6: SPEAKERS / speaker list
    - Rest: Alternating speaker+timestamp and text
    """
    doc = DocxDocument(docx_path)
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]

    if len(paragraphs) < 3:
        return None

    result = {
        'title': paragraphs[0] if paragraphs else Path(docx_path).stem,
        'recording_date': None,
        'duration_seconds': None,
        'keywords': [],
        'speakers': [],
        'segments': []
    }

    # Parse date and duration from line 2
    # Format: "Fri, Dec 05, 2025 10:47AM • 21:01"
    if len(paragraphs) > 1:
        date_line = paragraphs[1]

        # Extract date
        date_patterns = [
            r'(\w+, \w+ \d+, \d{4})',  # "Fri, Dec 05, 2025"
            r'(\d{4}-\d{2}-\d{2})',     # "2025-12-05"
            r'(\d{8})',                  # "20231127"
        ]
        for pattern in date_patterns:
            match = re.search(pattern, date_line)
            if match:
                try:
                    date_str = match.group(1)
                    if ',' in date_str:
                        result['recording_date'] = datetime.strptime(date_str, '%a, %b %d, %Y').date()
                    elif '-' in date_str:
                        result['recording_date'] = datetime.strptime(date_str, '%Y-%m-%d').date()
                    else:
                        result['recording_date'] = datetime.strptime(date_str, '%Y%m%d').date()
                    break
                except:
                    pass

        # Extract duration (format: "21:01" or "1:21:01")
        duration_match = re.search(r'(\d+:\d+(?::\d+)?)\s*$', date_line)
        if duration_match:
            time_str = duration_match.group(1)
            parts = time_str.split(':')
            if len(parts) == 2:
                result['duration_seconds'] = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                result['duration_seconds'] = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

    # Parse keywords and speakers
    i = 2
    while i < len(paragraphs) and i < 10:
        line = paragraphs[i]
        if line == 'SUMMARY KEYWORDS' and i + 1 < len(paragraphs):
            result['keywords'] = [k.strip() for k in paragraphs[i + 1].split(',')]
            i += 2
        elif line == 'SPEAKERS' and i + 1 < len(paragraphs):
            result['speakers'] = [s.strip() for s in paragraphs[i + 1].split(',')]
            i += 2
        else:
            i += 1

    # Parse transcript segments
    # Pattern: "Speaker Name  00:16" or just "00:16"
    timestamp_pattern = re.compile(r'^(.+?)?\s*(\d+:\d+(?::\d+)?)\s*$')

    segments = []
    current_speaker = None
    current_start = None
    current_text = []
    segment_index = 0

    for para in paragraphs[6:]:  # Skip header lines
        match = timestamp_pattern.match(para)
        if match:
            # Save previous segment if exists
            if current_start is not None and current_text:
                segments.append({
                    'segment_index': segment_index,
                    'start_time': current_start,
                    'speaker': current_speaker,
                    'text': ' '.join(current_text)
                })
                segment_index += 1

            # Parse new segment
            speaker_part = match.group(1)
            time_part = match.group(2)

            if speaker_part:
                current_speaker = speaker_part.strip()

            # Convert timestamp to seconds
            time_parts = time_part.split(':')
            if len(time_parts) == 2:
                current_start = int(time_parts[0]) * 60 + int(time_parts[1])
            elif len(time_parts) == 3:
                current_start = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])

            current_text = []
        else:
            # This is transcript text
            if para and not para.startswith('SUMMARY') and not para.startswith('SPEAKERS'):
                current_text.append(para)

    # Don't forget the last segment
    if current_start is not None and current_text:
        segments.append({
            'segment_index': segment_index,
            'start_time': current_start,
            'speaker': current_speaker,
            'text': ' '.join(current_text)
        })

    # Calculate end times (next segment's start or duration)
    for i, seg in enumerate(segments):
        if i + 1 < len(segments):
            seg['end_time'] = segments[i + 1]['start_time']
        elif result['duration_seconds']:
            seg['end_time'] = result['duration_seconds']
        else:
            seg['end_time'] = seg['start_time'] + 30  # Default 30 seconds

    result['segments'] = segments
    return result


def upload_to_s3(local_path: str, s3_key: str, bucket: str = 'mv-brain') -> bool:
    """Upload file to S3."""
    try:
        s3 = boto3.client('s3')
        s3.upload_file(local_path, bucket, s3_key)
        return True
    except Exception as e:
        print(f"  Error uploading to S3: {e}")
        return False


def get_file_size(path: str) -> int:
    """Get file size in bytes."""
    return os.path.getsize(path)


def import_otter_export(export_dir: str, persona_name: str = None, upload_audio: bool = True):
    """Import all Otter AI exports from a directory.

    Args:
        export_dir: Directory containing .docx and .mp3 file pairs
        persona_name: Optional persona name to associate recordings with
        upload_audio: Whether to upload mp3 files to S3
    """
    export_path = Path(export_dir)

    # Find all docx files
    docx_files = list(export_path.glob('*.docx'))
    print(f"Found {len(docx_files)} docx files in {export_dir}")

    session = get_session()

    # Get persona if specified
    persona_id = None
    if persona_name:
        persona = session.query(Persona).filter(Persona.name.ilike(f'%{persona_name}%')).first()
        if persona:
            persona_id = persona.id
            print(f"Associating with persona: {persona.name}")

    imported = 0
    skipped = 0
    errors = 0

    for docx_path in docx_files:
        filename_stem = docx_path.stem
        mp3_path = docx_path.with_suffix('.mp3')

        print(f"\nProcessing: {filename_stem}")

        # Check if mp3 exists
        if not mp3_path.exists():
            print(f"  Warning: No matching mp3 file, skipping")
            skipped += 1
            continue

        # Check if already imported (by original filename)
        existing = session.query(AudioRecording).filter(
            AudioRecording.original_filename == mp3_path.name
        ).first()
        if existing:
            print(f"  Already imported, skipping")
            skipped += 1
            continue

        # Parse docx
        try:
            parsed = parse_otter_docx(str(docx_path))
            if not parsed:
                print(f"  Error: Could not parse docx")
                errors += 1
                continue
        except Exception as e:
            print(f"  Error parsing docx: {e}")
            errors += 1
            continue

        # Generate S3 key
        safe_filename = re.sub(r'[^\w\-_.]', '_', filename_stem)
        s3_key = f"audio/otter_ai/{safe_filename}_{uuid.uuid4().hex[:8]}.mp3"

        # Upload to S3
        if upload_audio:
            print(f"  Uploading to S3: {s3_key}")
            if not upload_to_s3(str(mp3_path), s3_key):
                errors += 1
                continue

        # Create AudioRecording
        recording = AudioRecording(
            filename=f"{safe_filename}.mp3",
            original_filename=mp3_path.name,
            s3_key=s3_key,
            s3_bucket='mv-brain',
            file_size_bytes=get_file_size(str(mp3_path)),
            duration_seconds=parsed['duration_seconds'],
            format='mp3',
            title=parsed['title'],
            recording_date=parsed['recording_date'],
            speakers=parsed['speakers'],
            keywords=parsed['keywords'],
            persona_id=persona_id,
            source='otter_ai',
            status='transcribed'
        )
        session.add(recording)
        session.flush()  # Get the ID

        # Create segments
        for seg in parsed['segments']:
            segment = AudioSegment(
                audio_id=recording.id,
                segment_index=seg['segment_index'],
                start_time=Decimal(str(seg['start_time'])),
                end_time=Decimal(str(seg['end_time'])),
                text=seg['text'],
                speaker=seg['speaker']
            )
            session.add(segment)

        session.commit()
        print(f"  Imported: {len(parsed['segments'])} segments")
        imported += 1

    session.close()
    print(f"\n=== Import Complete ===")
    print(f"Imported: {imported}")
    print(f"Skipped: {skipped}")
    print(f"Errors: {errors}")


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("Usage: python import_otter_ai.py <export_dir> [persona_name] [--no-upload]")
        print("\nExample:")
        print("  python import_otter_ai.py /path/to/otter-export 'Dan Goldin'")
        print("  python import_otter_ai.py /path/to/otter-export --no-upload")
        sys.exit(1)

    export_dir = sys.argv[1]
    persona_name = None
    upload_audio = True

    for arg in sys.argv[2:]:
        if arg == '--no-upload':
            upload_audio = False
        else:
            persona_name = arg

    import_otter_export(export_dir, persona_name, upload_audio)

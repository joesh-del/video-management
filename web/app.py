"""Flask web application for video management dashboard."""

import os
import sys
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import UUID

from flask import Flask, render_template, jsonify, request, send_file, redirect, url_for, session
from openai import OpenAI
import anthropic

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
from scripts.db import DatabaseSession, Video, Transcript, TranscriptSegment, CompiledVideo, ScriptFeedback, Conversation, ChatMessage, User, AILog, Persona, Document, SocialPost, AudioRecording, AudioSegment
import time
import hashlib
import pyotp
import qrcode
import io
import base64
from scripts.config_loader import get_config


def get_s3_client():
    """Get configured S3 client."""
    config = get_config()
    return boto3.client(
        "s3",
        aws_access_key_id=config.secrets.get("aws", {}).get("access_key_id"),
        aws_secret_access_key=config.secrets.get("aws", {}).get("secret_access_key"),
        region_name=config.settings.get("aws", {}).get("region", "us-east-1"),
    )

app = Flask(__name__)

# Session configuration - persistent sessions that survive browser close
# Secret key is fixed so sessions persist across server restarts
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'mv-internal-secret-key-2026-change-in-production')
app.config['SESSION_COOKIE_SECURE'] = True  # HTTPS only
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Prevent XSS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 604800  # 7 days in seconds


def format_duration(seconds):
    """Format seconds as MM:SS or HH:MM:SS."""
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_timestamp(dt):
    """Format datetime for display."""
    if dt is None:
        return "--"
    return dt.strftime("%b %d, %Y %H:%M")


app.jinja_env.filters['duration'] = format_duration
app.jinja_env.filters['timestamp'] = format_timestamp


@app.route('/')
def index():
    """Dashboard home page."""
    with DatabaseSession() as db_session:
        video_count = db_session.query(Video).count()
        transcript_count = db_session.query(Transcript).filter(Transcript.status == "completed").count()
        persona_count = db_session.query(Persona).filter(Persona.is_active == 1).count()
        document_count = db_session.query(Document).count()

    return render_template('index.html',
                         video_count=video_count,
                         transcript_count=transcript_count,
                         persona_count=persona_count,
                         document_count=document_count)


@app.route('/videos')
def videos():
    """List all videos."""
    search_query = request.args.get('q', '').strip()

    with DatabaseSession() as session:
        query = session.query(Video)

        if search_query:
            search_filter = f'%{search_query}%'
            query = query.filter(
                (Video.speaker.ilike(search_filter)) |
                (Video.event_name.ilike(search_filter)) |
                (Video.filename.ilike(search_filter)) |
                (Video.description.ilike(search_filter))
            )

        videos = query.order_by(Video.created_at.desc()).all()
        video_list = []
        for v in videos:
            transcript = session.query(Transcript).filter(
                Transcript.video_id == v.id,
                Transcript.status == "completed"
            ).first()
            video_list.append({
                'id': str(v.id),
                'filename': v.filename,
                'duration': v.duration_seconds,
                'status': v.status,
                'created_at': v.created_at,
                'has_transcript': transcript is not None,
                's3_key': v.s3_key,
                'speaker': getattr(v, 'speaker', None),
                'event_name': getattr(v, 'event_name', None),
                'event_date': getattr(v, 'event_date', None),
                'description': getattr(v, 'description', None),
                'extra_data': getattr(v, 'extra_data', None) or {},
            })

    return render_template('videos.html', videos=video_list, search_query=search_query)


@app.route('/transcripts')
def transcripts():
    """List all transcripts."""
    with DatabaseSession() as session:
        transcript_list = []
        transcripts = session.query(Transcript).filter(
            Transcript.status == "completed"
        ).order_by(Transcript.created_at.desc()).all()

        for t in transcripts:
            video = session.query(Video).filter(Video.id == t.video_id).first()
            segment_count = session.query(TranscriptSegment).filter(
                TranscriptSegment.transcript_id == t.id
            ).count()
            transcript_list.append({
                'id': str(t.id),
                'video_id': str(t.video_id),
                'video_title': video.filename if video else "Unknown",
                'segment_count': segment_count,
                'created_at': t.created_at,
            })

    return render_template('transcripts.html', transcripts=transcript_list)


@app.route('/transcripts/<transcript_id>')
def transcript_detail(transcript_id):
    """View a single transcript with all segments."""
    with DatabaseSession() as session:
        transcript = session.query(Transcript).filter(
            Transcript.id == transcript_id
        ).first()

        if not transcript:
            return "Transcript not found", 404

        video = session.query(Video).filter(Video.id == transcript.video_id).first()
        segments = session.query(TranscriptSegment).filter(
            TranscriptSegment.transcript_id == transcript.id
        ).order_by(TranscriptSegment.start_time).all()

        # Get known speakers from video metadata
        known_speaker = video.speaker if video else None

        segment_list = [{
            'id': str(s.id),
            'start': float(s.start_time),
            'end': float(s.end_time),
            'text': s.text,
            'speaker': s.speaker or None,
        } for s in segments]

        return render_template('transcript_detail.html',
                             transcript_id=transcript_id,
                             video_id=str(video.id) if video else None,
                             video_title=video.filename if video else "Unknown",
                             known_speaker=known_speaker,
                             segments=segment_list,
                             total_duration=float(video.duration_seconds) if video and video.duration_seconds else 0)


@app.route('/transcripts/search')
def search_transcripts():
    """Search across all transcripts."""
    query = request.args.get('q', '').strip()
    results = []

    if query:
        with DatabaseSession() as session:
            segments = session.query(TranscriptSegment).filter(
                TranscriptSegment.text.ilike(f'%{query}%')
            ).limit(100).all()

            for seg in segments:
                transcript = session.query(Transcript).filter(
                    Transcript.id == seg.transcript_id
                ).first()
                if transcript:
                    video = session.query(Video).filter(
                        Video.id == transcript.video_id
                    ).first()
                    results.append({
                        'video_id': str(transcript.video_id),
                        'video_title': video.filename if video else "Unknown",
                        'transcript_id': str(transcript.id),
                        'start': float(seg.start_time),
                        'end': float(seg.end_time),
                        'text': seg.text,
                    })

    return render_template('search.html', query=query, results=results)


@app.route('/edit-scripts')
def edit_scripts():
    """List available edit scripts/clip folders."""
    clips_dir = Path('local_clips')
    scripts = []

    if clips_dir.exists():
        for folder in clips_dir.iterdir():
            if folder.is_dir():
                script_file = folder / 'EDIT_SCRIPT.txt'
                clip_count = len(list(folder.glob('*.mp4')))
                scripts.append({
                    'name': folder.name,
                    'has_script': script_file.exists(),
                    'clip_count': clip_count,
                })

    return render_template('edit_scripts.html', scripts=scripts)


@app.route('/edit-scripts/<name>')
def edit_script_detail(name):
    """View an edit script."""
    script_path = Path('local_clips') / name / 'EDIT_SCRIPT.txt'

    if not script_path.exists():
        return "Edit script not found", 404

    with open(script_path, 'r') as f:
        content = f.read()

    clips_dir = Path('local_clips') / name
    clips = sorted([f.name for f in clips_dir.glob('*.mp4')])

    return render_template('edit_script_detail.html',
                         name=name,
                         content=content,
                         clips=clips)


# ============================================================================
# CHAT INTERFACE FOR SCRIPT GENERATION
# ============================================================================

def get_openai_client():
    """Get OpenAI client."""
    config = get_config()
    api_key = config.secrets.get("openai", {}).get("api_key")
    if not api_key:
        raise ValueError("OpenAI API key not configured")
    return OpenAI(api_key=api_key)


def get_anthropic_client():
    """Get Anthropic client."""
    config = get_config()
    api_key = config.secrets.get("anthropic", {}).get("api_key")
    if not api_key:
        raise ValueError("Anthropic API key not configured. Add your key to config/secrets.yaml")
    return anthropic.Anthropic(api_key=api_key)


# Model mapping
MODEL_MAP = {
    # Claude models
    "claude-sonnet": "claude-sonnet-4-20250514",
    "claude-opus": "claude-opus-4-0-20250514",
    "claude-haiku": "claude-3-5-haiku-20241022",
    # OpenAI models
    "gpt-4o": "gpt-4o",
    "gpt-4-turbo": "gpt-4-turbo",
    "gpt-3.5-turbo": "gpt-3.5-turbo",
}


def log_ai_call(
    request_type: str,
    model: str,
    prompt: str = None,
    context_summary: str = None,
    response: str = None,
    clips_generated: int = 0,
    response_json: dict = None,
    success: bool = True,
    error_message: str = None,
    latency_ms: float = None,
    input_tokens: int = None,
    output_tokens: int = None,
    user_id: str = None,
    conversation_id: str = None
):
    """Log an AI API call for quality monitoring."""
    try:
        with DatabaseSession() as db_session:
            log_entry = AILog(
                request_type=request_type,
                model=model,
                prompt=prompt[:10000] if prompt else None,  # Truncate very long prompts
                context_summary=context_summary[:5000] if context_summary else None,
                response=response[:50000] if response else None,  # Keep full response for quality review
                clips_generated=clips_generated,
                response_json=response_json,
                success=1 if success else 0,
                error_message=error_message,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                user_id=UUID(user_id) if user_id else None,
                conversation_id=UUID(conversation_id) if conversation_id else None
            )
            db_session.add(log_entry)
            db_session.commit()
            print(f"[AI_LOG] {request_type} | model={model} | success={success} | latency={latency_ms:.0f}ms | clips={clips_generated}")
    except Exception as e:
        print(f"[AI_LOG ERROR] Failed to log AI call: {e}")


def search_transcripts_for_context(query: str, limit: int = 1000):
    """Search transcripts for relevant segments based on query keywords.

    IMPROVED: Prioritizes rare/specific keywords over common words.
    Results matching 'caterpillar' rank higher than those matching 'how'.
    """
    # Extract keywords from query - get meaningful words (3+ chars for better matching)
    stop_words = {'want', 'need', 'like', 'make', 'create', 'find', 'give', 'about', 'from', 'with', 'that', 'this', 'have', 'will', 'would', 'could', 'should', 'video', 'script', 'clips', 'second', 'minute', 'the', 'and', 'for', 'how', 'know', 'talking', 'talk', 'good', 'great', 'thing', 'things', 'way', 'just', 'really', 'very', 'also', 'can', 'get', 'got', 'say', 'said', 'think', 'going', 'look', 'see', 'time', 'year', 'years', 'people', 'work', 'working'}
    keywords = [w for w in re.findall(r'\b\w{3,}\b', query.lower()) if w not in stop_words]

    # Remove duplicates while preserving order
    keywords = list(dict.fromkeys(keywords))

    # Debug: log keywords
    print(f"[DEBUG] Search keywords: {keywords}")

    # Extract year filters from query (e.g., "2020", "2023", "from 2020")
    year_pattern = re.findall(r'\b(19\d{2}|20\d{2})\b', query)
    min_year = None
    if year_pattern:
        min_year = min(int(y) for y in year_pattern)

    # Check for "recent" or "latest" keywords
    if any(w in query.lower() for w in ['recent', 'latest', 'new', 'newest']):
        min_year = 2020

    results = []
    results_by_id = {}  # Track by segment_id to avoid duplicates and merge scores

    with DatabaseSession() as db_session:
        # Get all videos with their full metadata
        videos = db_session.query(Video).all()
        video_map = {str(v.id): {
            'filename': v.filename,
            'speaker': v.speaker or 'Unknown',
            'event_name': v.event_name or 'Unknown',
            'event_date': v.event_date,
            'year': v.event_date.year if v.event_date else None
        } for v in videos}

        # Filter videos by year if specified - but be inclusive
        # Include videos without dates to avoid missing content
        if min_year:
            filtered_video_ids = {
                str(v.id) for v in videos
                if (v.event_date and v.event_date.year >= min_year) or not v.event_date
            }
        else:
            filtered_video_ids = set(video_map.keys())

        # First pass: count how many results each keyword returns (to identify rare keywords)
        keyword_counts = {}
        for keyword in keywords[:15]:
            count = db_session.query(TranscriptSegment).join(
                Transcript, TranscriptSegment.transcript_id == Transcript.id
            ).filter(
                TranscriptSegment.text.ilike(f'%{keyword}%'),
                Transcript.status == 'completed'
            ).count()
            keyword_counts[keyword] = count
            print(f"[DEBUG] Keyword '{keyword}' has {count} matches")

        # Calculate keyword weights - rare keywords get higher weight
        # A keyword with 10 matches is more specific than one with 100
        keyword_weights = {}
        for kw, count in keyword_counts.items():
            if count == 0:
                keyword_weights[kw] = 0
            elif count <= 5:
                keyword_weights[kw] = 10  # Very specific - highest priority
            elif count <= 20:
                keyword_weights[kw] = 5   # Specific
            elif count <= 50:
                keyword_weights[kw] = 2   # Moderate
            else:
                keyword_weights[kw] = 1   # Common

        print(f"[DEBUG] Keyword weights: {keyword_weights}")

        # Search for matching segments with scoring
        for keyword in keywords[:15]:
            if keyword_counts.get(keyword, 0) == 0:
                continue

            segments = db_session.query(TranscriptSegment).join(
                Transcript, TranscriptSegment.transcript_id == Transcript.id
            ).filter(
                TranscriptSegment.text.ilike(f'%{keyword}%'),
                Transcript.status == 'completed'
            ).limit(100).all()

            for seg in segments:
                transcript = db_session.query(Transcript).filter(
                    Transcript.id == seg.transcript_id
                ).first()
                if transcript and str(transcript.video_id) in filtered_video_ids:
                    video_info = video_map.get(str(transcript.video_id), {})
                    event_date = video_info.get('event_date')
                    date_str = event_date.strftime('%Y-%m-%d') if event_date else 'Unknown date'

                    # Get surrounding segments for more complete context (30 seconds around match)
                    nearby_segments = db_session.query(TranscriptSegment).filter(
                        TranscriptSegment.transcript_id == seg.transcript_id,
                        TranscriptSegment.start_time >= float(seg.start_time) - 15,
                        TranscriptSegment.end_time <= float(seg.end_time) + 15
                    ).order_by(TranscriptSegment.start_time).all()

                    if nearby_segments:
                        combined_text = ' '.join(s.text for s in nearby_segments)
                        start_time = float(nearby_segments[0].start_time)
                        end_time = float(nearby_segments[-1].end_time)
                    else:
                        combined_text = seg.text
                        start_time = float(seg.start_time)
                        end_time = float(seg.end_time)

                    seg_key = str(seg.id)

                    if seg_key in results_by_id:
                        # Already have this segment - add to its score
                        results_by_id[seg_key]['score'] += keyword_weights.get(keyword, 1)
                        results_by_id[seg_key]['matched_keywords'].add(keyword)
                    else:
                        # New segment
                        results_by_id[seg_key] = {
                            'video_id': str(transcript.video_id),
                            'video_title': video_info.get('filename', 'Unknown'),
                            'speaker': video_info.get('speaker', 'Unknown'),
                            'event_name': video_info.get('event_name', 'Unknown'),
                            'event_date': date_str,
                            'year': video_info.get('year'),
                            'start': start_time,
                            'end': end_time,
                            'text': combined_text,
                            'segment_id': seg_key,
                            'score': keyword_weights.get(keyword, 1),
                            'matched_keywords': {keyword}
                        }

        # Sort results by score (highest first) - rare keyword matches come first
        results = sorted(results_by_id.values(), key=lambda x: -x['score'])

        # Log top results
        print(f"[DEBUG] Total unique results: {len(results)}")
        if results:
            top = results[0]
            print(f"[DEBUG] Top result (score={top['score']}, keywords={top['matched_keywords']}): {top['text'][:80]}...")

        # If no keyword matches, get a sample of transcripts (respecting year filter)
        if not results:
            transcripts = db_session.query(Transcript).filter(
                Transcript.status == 'completed'
            ).all()

            # Filter by year
            transcripts = [t for t in transcripts if str(t.video_id) in filtered_video_ids][:20]

            for t in transcripts:
                video_info = video_map.get(str(t.video_id), {})
                event_date = video_info.get('event_date')
                date_str = event_date.strftime('%Y-%m-%d') if event_date else 'Unknown date'

                segments = db_session.query(TranscriptSegment).filter(
                    TranscriptSegment.transcript_id == t.id
                ).order_by(TranscriptSegment.start_time).limit(30).all()

                for seg in segments:
                    if len(seg.text) > 40:  # Only substantial segments
                        results.append({
                            'video_id': str(t.video_id),
                            'video_title': video_info.get('filename', 'Unknown'),
                            'speaker': video_info.get('speaker', 'Unknown'),
                            'event_name': video_info.get('event_name', 'Unknown'),
                            'event_date': date_str,
                            'year': video_info.get('year'),
                            'start': float(seg.start_time),
                            'end': float(seg.end_time),
                            'text': seg.text,
                            'segment_id': str(seg.id)
                        })

    # Deduplicate and limit
    seen = set()
    unique_results = []
    for r in results:
        key = (r['video_id'], r['start'], r['end'])
        if key not in seen:
            seen.add(key)
            unique_results.append(r)
            if len(unique_results) >= limit:
                break

    print(f"[DEBUG] Total unique results: {len(unique_results)}")
    if unique_results:
        print(f"[DEBUG] Sample result: {unique_results[0]['text'][:100]}...")

    return unique_results


def search_audio_for_context(query: str, limit: int = 100):
    """Search audio recordings for relevant segments based on query keywords.

    Returns audio segments with S3 URLs for playback, similar to video transcript search.
    """
    stop_words = {'want', 'need', 'like', 'make', 'create', 'find', 'give', 'about', 'from', 'with', 'that', 'this', 'have', 'will', 'would', 'could', 'should', 'audio', 'clip', 'clips', 'second', 'minute', 'the', 'and', 'for', 'how', 'know', 'talking', 'talk', 'good', 'great', 'thing', 'things', 'way', 'just', 'really', 'very', 'also', 'can', 'get', 'got', 'say', 'said', 'think', 'going', 'look', 'see', 'time', 'year', 'years', 'people', 'work', 'working'}
    keywords = [w for w in re.findall(r'\b\w{3,}\b', query.lower()) if w not in stop_words]
    keywords = list(dict.fromkeys(keywords))

    results = []
    results_by_id = {}

    with DatabaseSession() as db_session:
        # Get audio recording metadata
        recordings = db_session.query(AudioRecording).filter(
            AudioRecording.status == 'transcribed'
        ).all()
        recording_map = {str(r.id): {
            'title': r.title,
            'filename': r.filename,
            's3_key': r.s3_key,
            's3_bucket': r.s3_bucket,
            'speakers': r.speakers or [],
            'recording_date': r.recording_date,
            'duration': float(r.duration_seconds) if r.duration_seconds else None
        } for r in recordings}

        # Search for matching segments
        for keyword in keywords[:10]:
            segments = db_session.query(AudioSegment).filter(
                AudioSegment.text.ilike(f'%{keyword}%')
            ).limit(50).all()

            for seg in segments:
                audio_id = str(seg.audio_id)
                if audio_id not in recording_map:
                    continue

                audio_info = recording_map[audio_id]
                recording_date = audio_info.get('recording_date')
                date_str = recording_date.strftime('%Y-%m-%d') if recording_date else 'Unknown date'

                seg_key = str(seg.id)
                if seg_key in results_by_id:
                    results_by_id[seg_key]['score'] += 1
                    results_by_id[seg_key]['matched_keywords'].add(keyword)
                else:
                    # Generate presigned URL for audio clip
                    s3_key = audio_info.get('s3_key')

                    results_by_id[seg_key] = {
                        'type': 'audio',
                        'audio_id': audio_id,
                        'audio_title': audio_info.get('title', 'Unknown'),
                        'filename': audio_info.get('filename'),
                        's3_key': s3_key,
                        's3_bucket': audio_info.get('s3_bucket', 'mv-brain'),
                        'speaker': seg.speaker or (audio_info.get('speakers', ['Unknown'])[0] if audio_info.get('speakers') else 'Unknown'),
                        'recording_date': date_str,
                        'start': float(seg.start_time),
                        'end': float(seg.end_time),
                        'text': seg.text,
                        'segment_id': seg_key,
                        'score': 1,
                        'matched_keywords': {keyword}
                    }

        # Sort by score
        results = sorted(results_by_id.values(), key=lambda x: -x['score'])

    # Deduplicate and limit
    seen = set()
    unique_results = []
    for r in results:
        key = (r['audio_id'], r['start'], r['end'])
        if key not in seen:
            seen.add(key)
            unique_results.append(r)
            if len(unique_results) >= limit:
                break

    print(f"[DEBUG] Audio search: {len(unique_results)} results for keywords: {keywords[:5]}")
    return unique_results


def validate_clips_against_database(clips: list) -> list:
    """Validate that clips reference real videos AND that the text actually exists in transcripts."""
    validated = []

    with DatabaseSession() as db_session:
        # Pre-load all video IDs for fuzzy matching
        all_videos = db_session.query(Video).all()
        video_id_map = {str(v.id): v for v in all_videos}
        video_title_map = {v.filename.lower(): v for v in all_videos}

        for clip in clips:
            video_id = clip.get('video_id')
            video_title = clip.get('video_title', '')
            start_time = clip.get('start_time', 0)
            end_time = clip.get('end_time', 0)
            claimed_text = clip.get('text', '')

            video = None

            # 1. Try exact video ID match
            try:
                video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            except:
                pass

            # 2. If not found, try fuzzy ID matching (1-2 character difference)
            if not video and video_id:
                for existing_id, existing_video in video_id_map.items():
                    # Compare IDs character by character
                    if len(existing_id) == len(video_id):
                        diff_count = sum(1 for a, b in zip(existing_id, video_id) if a != b)
                        if diff_count <= 2:  # Allow up to 2 character differences
                            video = existing_video
                            print(f"[FUZZY MATCH] ID {video_id} -> {existing_id} (diff: {diff_count})")
                            break

            # 3. If still not found, try matching by video title
            if not video and video_title:
                title_lower = video_title.lower()
                # Exact title match
                if title_lower in video_title_map:
                    video = video_title_map[title_lower]
                    print(f"[TITLE MATCH] Found video by exact title: {video_title}")
                else:
                    # Partial title match - find videos containing the title
                    for filename, v in video_title_map.items():
                        if title_lower in filename or filename in title_lower:
                            video = v
                            print(f"[TITLE MATCH] Found video by partial title: {video_title} -> {v.filename}")
                            break

            if not video:
                print(f"[VALIDATION] No video found for ID: {video_id}, title: {video_title}")
                continue

            # Find matching or nearby segments
            transcript = db_session.query(Transcript).filter(
                Transcript.video_id == video.id,
                Transcript.status == 'completed'
            ).first()

            if not transcript:
                continue

            # Search for the claimed text in the transcript
            # Extract key phrase to search for - use multiple words for better matching
            search_phrase = claimed_text[:100].strip().lower()
            # Remove common starting words and punctuation
            for prefix in ['"', "'", 'and ', 'but ', 'so ', 'the ', 'that ', 'we ', 'i ', 'you ']:
                if search_phrase.startswith(prefix):
                    search_phrase = search_phrase[len(prefix):]
            # Extract meaningful words (skip first word if it's short)
            words = search_phrase.split()
            if len(words) > 3:
                # Use 3-4 consecutive words for matching
                search_phrase = ' '.join(words[1:4]) if len(words[0]) < 4 else ' '.join(words[:3])
            else:
                search_phrase = ' '.join(words[:3])

            # Search for this text in transcript segments
            matching_segments = db_session.query(TranscriptSegment).filter(
                TranscriptSegment.transcript_id == transcript.id,
                TranscriptSegment.text.ilike(f'%{search_phrase}%')
            ).order_by(TranscriptSegment.start_time).all()

            if matching_segments:
                # Found matching text - use the actual segment data
                seg = matching_segments[0]

                # Get surrounding segments for MORE complete context (30 seconds)
                nearby = db_session.query(TranscriptSegment).filter(
                    TranscriptSegment.transcript_id == transcript.id,
                    TranscriptSegment.start_time >= float(seg.start_time) - 15,
                    TranscriptSegment.end_time <= float(seg.end_time) + 15
                ).order_by(TranscriptSegment.start_time).all()

                actual_text = ' '.join(s.text for s in nearby)
                actual_start = float(nearby[0].start_time)
                actual_end = float(nearby[-1].end_time)
                event_date = video.event_date.strftime('%Y-%m-%d') if video.event_date else 'Unknown'

                validated.append({
                    'video_id': str(video.id),
                    'video_title': video.filename,
                    'speaker': video.speaker or 'Unknown',
                    'event_name': video.event_name or 'Unknown',
                    'event_date': event_date,
                    'start_time': actual_start,
                    'end_time': actual_end,
                    'duration': actual_end - actual_start,
                    'text': actual_text,
                    'verified': True,
                    'original_text': claimed_text
                })
            # If text not found, skip this clip entirely (it's hallucinated)

    return validated


def clean_clip_text(text: str) -> str:
    """Clean clip text - minimal processing to preserve original content."""
    text = text.strip()

    if not text:
        return "..."

    # Just trim to reasonable length if too long
    if len(text) > 500:
        # Find a good stopping point
        text = text[:500]
        last_period = text.rfind('.')
        if last_period > 200:
            text = text[:last_period + 1]

    return text


def reconstruct_script_with_verified_clips(ai_response: str, verified_clips: list) -> str:
    """Rebuild the script using verified clip texts so script matches actual clips."""

    # Extract title from AI response
    title_match = re.search(r'\*\*\[(.*?)\]\*\*', ai_response)
    title = title_match.group(1) if title_match else "Video Script"

    # Extract all RECORD sections (narration to record)
    records = re.findall(r'\[RECORD:\s*(.*?)\]', ai_response)
    # Also try old format
    if not records:
        records = re.findall(r'\[NARRATE:\s*(.*?)\]', ai_response)

    # Build new script with verified clips
    script = f"**[{title}]**\n\n"

    record_idx = 0

    # Add opening narration if exists
    if records and record_idx < len(records):
        script += f"[RECORD: {records[record_idx]}]\n\n"
        record_idx += 1

    # Add each verified clip with narration bridges
    for i, clip in enumerate(verified_clips):
        # Use the ACTUAL verified text from the database, cleaned up
        raw_text = clip.get('text', '').strip()
        clip_text = clean_clip_text(raw_text)

        # Store the cleaned text back for display consistency
        clip['display_text'] = clip_text

        # Get speaker info if available
        speaker = clip.get('speaker', '')
        if speaker and speaker != 'Unknown':
            script += f'[VIDEO: "{clip_text}" — {speaker}]\n\n'
        else:
            script += f'[VIDEO: "{clip_text}"]\n\n'

        # Add transition narration if available
        if record_idx < len(records):
            script += f"[RECORD: {records[record_idx]}]\n\n"
            record_idx += 1

    return script


def generate_script_with_ai(user_message: str, transcript_context: list, conversation_history: list, model: str = "gpt-4o", exclude_clips: list = None, user_id: str = None, conversation_id: str = None):
    """Generate a script using AI with verified transcript data."""

    # Build summary of available content
    speakers = set()
    events = set()
    years = set()
    for t in transcript_context:
        speakers.add(t.get('speaker', 'Unknown'))
        events.add(t.get('event_name', 'Unknown'))
        if t.get('year'):
            years.add(t['year'])

    summary = f"""AVAILABLE CONTENT SUMMARY:
- Speakers: {', '.join(sorted(speakers))}
- Events: {', '.join(sorted(events)[:10])}{'...' if len(events) > 10 else ''}
- Years: {min(years) if years else 'N/A'} to {max(years) if years else 'N/A'}
- Total clips available: {len(transcript_context)}
"""

    # Fetch good AND bad examples from database (few-shot learning)
    examples_text = ""
    try:
        with DatabaseSession() as db_session:
            # Good examples - learn from these
            good_scripts = db_session.query(ScriptFeedback).filter(
                ScriptFeedback.rating == 1
            ).order_by(ScriptFeedback.created_at.desc()).limit(2).all()

            if good_scripts:
                examples_text = "\n\nHERE ARE EXAMPLES OF GOOD SCRIPTS (learn from this style):\n"
                for i, ex in enumerate(good_scripts, 1):
                    script_clean = ex.script.split('```json')[0].strip() if '```json' in ex.script else ex.script
                    examples_text += f"\n--- GOOD EXAMPLE {i} ---\nUser asked: \"{ex.query}\"\nGood response:\n{script_clean[:1500]}...\n"

            # Bad examples - AVOID these mistakes
            bad_scripts = db_session.query(ScriptFeedback).filter(
                ScriptFeedback.rating == -1
            ).order_by(ScriptFeedback.created_at.desc()).limit(2).all()

            if bad_scripts:
                examples_text += "\n\nHERE ARE EXAMPLES OF BAD SCRIPTS (DO NOT make these mistakes):\n"
                for i, ex in enumerate(bad_scripts, 1):
                    script_clean = ex.script.split('```json')[0].strip() if '```json' in ex.script else ex.script
                    examples_text += f"\n--- BAD EXAMPLE {i} (AVOID THIS) ---\nUser asked: \"{ex.query}\"\nBad response (DO NOT DO THIS):\n{script_clean[:1000]}...\n"
    except Exception as e:
        print(f"[DEBUG] Could not fetch examples: {e}")

    # Build exclusion list from previously used clips
    exclude_text = ""
    if exclude_clips:
        exclude_text = "\n\nCLIPS TO EXCLUDE (DO NOT USE THESE - already used in previous scripts):\n"
        for clip in exclude_clips:
            exclude_text += f"- video_id: {clip.get('video_id')} | {clip.get('start_time', 0):.1f}s-{clip.get('end_time', 0):.1f}s | \"{clip.get('text', '')[:80]}...\"\n"
        exclude_text += "\nYou MUST use DIFFERENT clips than the ones listed above.\n"

    # Build context from transcripts
    # GPT-4o has 30k token limit, Claude has 200k - adjust accordingly
    is_claude = model.startswith("claude")
    max_segments = 300 if is_claude else 80  # Claude can handle more context
    max_chars = 100000 if is_claude else 20000  # Rough char limits

    context_text = ""
    seen_passages = set()
    total_chars = 0

    for t in transcript_context[:max_segments]:
        passage_key = (t['video_id'], round(t['start'], 0))
        if passage_key in seen_passages:
            continue
        seen_passages.add(passage_key)

        text = t["text"].strip()

        # Only skip truly unusable segments (very short)
        if len(text) < 15:
            continue

        # Check if we'd exceed character limit
        entry = f"[{t.get('event_date', 'Unknown')} | {t.get('speaker', 'Unknown')}]\n"
        entry += f"Video: {t['video_title']} | {t['start']:.1f}s-{t['end']:.1f}s | ID:{t['video_id']}\n"
        entry += f'"{text}"\n\n'

        if total_chars + len(entry) > max_chars:
            break

        context_text += entry
        total_chars += len(entry)

    system_prompt = f"""You are a video script creator. You have access to a database of transcript segments below.

{summary}

IMPORTANT RULES:
1. ALWAYS create a script using the transcript data provided below - NEVER say you don't have access or ask for more data
2. Copy video_id and timestamps EXACTLY from the data below
3. Look for COMPLETE THOUGHTS - prefer quotes that start with capital letters and end with periods
4. If a segment starts mid-sentence (with "and", "but", lowercase), look for a better starting point nearby
5. NEVER USE THE SAME CLIP TWICE - each video_id + timestamp combination must be unique in your script. No duplicates!
6. If user requests a SPECIFIC CLIP as the first clip (e.g., "The first clip should be..."), you MUST use that exact clip first. Search for it in the transcript data and use it verbatim.
7. Mix clips from DIFFERENT recording sessions/videos when possible - avoid using consecutive clips from the same video unless necessary

CRITICAL - [RECORD] NARRATION STYLE:
- Write ALL [RECORD] sections in FIRST PERSON as if the speaker (e.g. Dan Goldin) is narrating their own story
- Use "I", "my", "we" - NOT "he", "his", "Goldin said"
- Example BAD: "Dan Goldin developed a philosophy about caterpillars..."
- Example GOOD: "I developed a philosophy about spotting caterpillars..."
- The [RECORD] should sound like the speaker reflecting on their own experiences

CRITICAL - SMOOTH TRANSITIONS:
- Each [RECORD] must lead DIRECTLY into what the [VIDEO] clip will say
- Read the FIRST WORDS of the video clip and make the [RECORD] set them up
- Example: If video starts "The caterpillars are ugly..." then [RECORD] should end with something like "...and I learned to see their true nature:"
- NEVER write a [RECORD] that has no connection to the video clip that follows

OUTPUT FORMAT:

**[Title]**

[RECORD: First-person opening - "I..." or "When I..."]

[VIDEO: "Complete quote from transcript" | video_id | start-end]

[RECORD: First-person bridge that leads into next clip]

[VIDEO: "Complete quote from transcript" | video_id | start-end]

[RECORD: First-person closing reflection]

After script, provide JSON:
```json
{{"title": "...", "clips": [{{"video_id": "ID", "video_title": "TITLE", "start_time": 0.0, "end_time": 0.0, "text": "TEXT"}}]}}
```

FINDING GOOD CLIPS:
- Look for segments that express complete ideas
- Prefer quotes that would make sense to someone who hasn't seen the full video
- If explaining a concept (like "frogs and caterpillars"), find the segments where it's DEFINED, not just mentioned
- Combine multiple short segments if they form a complete thought
{examples_text}
{exclude_text}
---

TRANSCRIPT DATA:
""" + context_text

    # Build messages
    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history (last 10 messages)
    for msg in conversation_history[-10:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    # Log the full prompt for debugging
    import os
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'ai_prompts.log')
    with open(log_file, 'a') as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"TIMESTAMP: {datetime.utcnow().isoformat()}\n")
        f.write(f"MODEL: {model}\n")
        f.write(f"USER MESSAGE: {user_message}\n")
        f.write(f"EXCLUDE CLIPS: {exclude_clips}\n")
        f.write(f"\n--- SYSTEM PROMPT (first 3000 chars) ---\n{system_prompt[:3000]}\n")
        f.write(f"\n--- FULL SYSTEM PROMPT LENGTH: {len(system_prompt)} chars ---\n")

    # Start timing for logging
    start_time = time.time()
    input_tokens = None
    output_tokens = None

    try:
        # Determine which provider to use
        actual_model = MODEL_MAP.get(model, model)
        is_claude = model.startswith("claude")

        if is_claude:
            # Use Anthropic Claude
            client = get_anthropic_client()
            # Claude uses system prompt differently
            response = client.messages.create(
                model=actual_model,
                max_tokens=4000,
                system=system_prompt,
                messages=[{"role": m["role"], "content": m["content"]} for m in messages[1:]]  # Skip system message
            )
            assistant_message = response.content[0].text
            # Extract token usage from Claude response
            if hasattr(response, 'usage'):
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
        else:
            # Use OpenAI
            client = get_openai_client()
            response = client.chat.completions.create(
                model=actual_model,
                messages=messages,
                temperature=0.3,
                max_tokens=2000,
            )
            assistant_message = response.choices[0].message.content
            # Extract token usage from OpenAI response
            if hasattr(response, 'usage'):
                input_tokens = response.usage.prompt_tokens
                output_tokens = response.usage.completion_tokens

        # Log the AI response
        with open(log_file, 'a') as f:
            f.write(f"\n--- AI RESPONSE ---\n{assistant_message}\n")

        # Try to extract JSON clips from response
        clips = []
        if "```json" in assistant_message:
            try:
                json_str = assistant_message.split("```json")[1].split("```")[0]
                data = json.loads(json_str)
                clips = data.get("clips", [])
            except:
                pass

        # Log extracted clips
        with open(log_file, 'a') as f:
            f.write(f"\n--- EXTRACTED CLIPS ({len(clips)}) ---\n")
            for i, c in enumerate(clips):
                f.write(f"  {i+1}. video_id={c.get('video_id')} | {c.get('start_time')}-{c.get('end_time')} | {c.get('text', '')[:60]}...\n")

        # Validate clips against database
        if clips:
            validated_clips = validate_clips_against_database(clips)
        else:
            validated_clips = []

        # DEDUPLICATE: Remove clips that overlap with each other (same video, overlapping times)
        if len(validated_clips) > 1:
            deduped_clips = []
            for clip in validated_clips:
                is_duplicate = False
                for existing in deduped_clips:
                    # Same video?
                    if clip.get('video_id') == existing.get('video_id'):
                        # Check for time overlap
                        clip_start = clip.get('start_time', 0)
                        clip_end = clip.get('end_time', 0)
                        exist_start = existing.get('start_time', 0)
                        exist_end = existing.get('end_time', 0)
                        # Overlap if: clip starts before existing ends AND clip ends after existing starts
                        if clip_start < exist_end + 5 and clip_end > exist_start - 5:  # 5s tolerance
                            is_duplicate = True
                            with open(log_file, 'a') as f:
                                f.write(f"  DUPLICATE REMOVED: {clip.get('video_id')} {clip_start}-{clip_end} overlaps with {exist_start}-{exist_end}\n")
                            break
                if not is_duplicate:
                    deduped_clips.append(clip)
            validated_clips = deduped_clips

        # Log validated clips
        with open(log_file, 'a') as f:
            f.write(f"\n--- VALIDATED CLIPS ({len(validated_clips)}) ---\n")
            for i, c in enumerate(validated_clips):
                f.write(f"  {i+1}. video_id={c.get('video_id')} | {c.get('start_time')}-{c.get('end_time')} | {c.get('text', '')[:60]}...\n")
            f.write(f"\n{'='*80}\n")

        # RECONSTRUCT the script using verified clip texts
        # This ensures the displayed script matches the actual clips
        if validated_clips:
            reconstructed = reconstruct_script_with_verified_clips(assistant_message, validated_clips)
            # Update clip texts to match what's shown in the script
            for clip in validated_clips:
                if 'display_text' in clip:
                    clip['text'] = clip['display_text']
        else:
            reconstructed = assistant_message

        # Log successful AI call to database
        latency_ms = (time.time() - start_time) * 1000
        log_ai_call(
            request_type="chat",
            model=actual_model,
            prompt=user_message,
            context_summary=f"Speakers: {', '.join(sorted(speakers)[:5])}; Events: {', '.join(sorted(events)[:3])}; {len(transcript_context)} segments",
            response=assistant_message,
            clips_generated=len(validated_clips),
            response_json={"clips": validated_clips} if validated_clips else None,
            success=True,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            user_id=user_id,
            conversation_id=conversation_id
        )

        return {
            "message": reconstructed,
            "clips": validated_clips,
            "has_script": len(validated_clips) > 0
        }

    except Exception as e:
        # Log failed AI call to database
        latency_ms = (time.time() - start_time) * 1000
        log_ai_call(
            request_type="chat",
            model=MODEL_MAP.get(model, model),
            prompt=user_message,
            context_summary=f"{len(transcript_context)} segments",
            success=False,
            error_message=str(e),
            latency_ms=latency_ms,
            user_id=user_id,
            conversation_id=conversation_id
        )

        return {
            "message": f"Error generating response: {str(e)}",
            "clips": [],
            "has_script": False,
            "error": True
        }


# ============================================================================
# COPY GENERATION (LinkedIn posts, tweets, etc.)
# ============================================================================

def detect_copy_intent(message: str) -> dict:
    """Detect if user wants copy generation vs video scripts.

    Returns:
        dict with keys:
        - is_copy: bool - True if user wants copy generation
        - persona_name: str or None - Name of persona mentioned
        - platform: str or None - Target platform (linkedin, x, email, etc.)
        - topic: str - The topic/subject matter
    """
    message_lower = message.lower()

    # Platform indicators
    platforms = {
        'linkedin': ['linkedin', 'linked in', 'linkedin post'],
        'x': ['tweet', 'x post', 'twitter'],
        'email': ['email', 'e-mail'],
        'blog': ['blog post', 'article'],
        'press': ['press release'],
    }

    # Copy action indicators
    copy_actions = ['write', 'draft', 'create', 'generate', 'compose', 'craft']
    copy_types = ['post', 'tweet', 'email', 'article', 'copy', 'content', 'message', 'release']

    # Check for copy intent
    is_copy = False
    detected_platform = None

    # Check for platform mentions
    for platform, keywords in platforms.items():
        for kw in keywords:
            if kw in message_lower:
                is_copy = True
                detected_platform = platform
                break
        if detected_platform:
            break

    # Check for copy action + type combinations
    if not is_copy:
        for action in copy_actions:
            for ctype in copy_types:
                if action in message_lower and ctype in message_lower:
                    is_copy = True
                    break
            if is_copy:
                break

    # Check for persona mention
    persona_name = None
    with DatabaseSession() as db_session:
        personas = db_session.query(Persona).filter(Persona.is_active == 1).all()
        for p in personas:
            if p.name.lower() in message_lower:
                persona_name = p.name
                break
            # Also check first name
            first_name = p.name.split()[0].lower()
            if len(first_name) > 2 and first_name in message_lower:
                persona_name = p.name
                break

    # If persona mentioned with "voice", "style", "as", it's likely copy
    if persona_name:
        voice_indicators = ["voice", "style", "as", "for", "like"]
        for ind in voice_indicators:
            if ind in message_lower:
                is_copy = True
                break

    return {
        'is_copy': is_copy,
        'persona_name': persona_name,
        'platform': detected_platform,
        'topic': message  # Will be refined by the AI
    }


def generate_copy_with_ai(
    user_message: str,
    persona_name: str,
    platform: str,
    transcript_context: list,
    conversation_history: list,
    model: str = "claude-sonnet",
    user_id: str = None,
    conversation_id: str = None
) -> dict:
    """Generate copy in a persona's voice using their content as reference."""

    start_time = time.time()

    # Load persona and extract all needed data within session
    with DatabaseSession() as db_session:
        persona = db_session.query(Persona).filter(
            Persona.name == persona_name,
            Persona.is_active == 1
        ).first()

        if not persona:
            return {
                "message": f"Persona '{persona_name}' not found.",
                "copy": None,
                "is_copy": True
            }

        # Extract all needed data while session is open
        p_name = persona.name
        p_description = persona.description or 'Not specified'
        p_tone = persona.tone or 'Not specified'
        p_style_notes = persona.style_notes or 'Not specified'
        p_topics = ', '.join(persona.topics) if persona.topics else 'Not specified'
        p_vocabulary = ', '.join(persona.vocabulary) if persona.vocabulary else 'Not specified'
        persona_id = persona.id

        # Load sample social posts if available
        sample_posts = db_session.query(SocialPost).filter(
            SocialPost.persona_id == persona_id
        ).order_by(SocialPost.posted_at.desc()).limit(5).all()

        samples_text = ""
        if sample_posts:
            samples_text = "\n\nEXAMPLE POSTS FROM THIS PERSONA (match this style):\n"
            for post in sample_posts:
                samples_text += f"\n[{post.platform}]: {post.content[:500]}\n"

    # Build persona voice profile (outside session, using extracted data)
    voice_profile = f"""PERSONA: {p_name}

DESCRIPTION: {p_description}

TONE: {p_tone}

STYLE NOTES: {p_style_notes}

KEY TOPICS: {p_topics}

VOCABULARY/PHRASES: {p_vocabulary}
"""

    # Build transcript context for reference material
    context_text = ""
    for t in transcript_context[:50]:  # Limit context
        text = t["text"].strip()
        if len(text) > 30:
            context_text += f'"{text}"\n\n'

    # Platform-specific instructions
    platform_instructions = {
        'linkedin': """LinkedIn Post Guidelines:
- Professional but personable tone
- Can be 1-3 paragraphs or use bullet points
- Often starts with a hook or personal insight
- May include a call to action or question
- Appropriate hashtags (2-5)""",
        'x': """X/Twitter Post Guidelines:
- Must be under 280 characters
- Punchy and memorable
- Can use thread format for longer thoughts (indicate with 1/, 2/, etc.)
- Hashtags sparingly (1-2)""",
        'email': """Email Guidelines:
- Clear subject line
- Professional greeting
- Concise body with clear purpose
- Appropriate closing""",
        'blog': """Blog Post Guidelines:
- Engaging headline
- Introduction that hooks the reader
- Clear structure with subheadings
- Conclusion with takeaway"""
    }

    platform_guide = platform_instructions.get(platform, "Write in a professional, engaging tone.")

    system_prompt = f"""You are a ghostwriter creating content in the voice of {p_name}.

{voice_profile}
{samples_text}

{platform_guide}

REFERENCE MATERIAL (use these as source material/inspiration):
{context_text[:15000]}

IMPORTANT:
1. Write EXACTLY as {p_name} would write - use first person ("I", "my", "we")
2. Match their tone, vocabulary, and style precisely
3. Draw on the reference material for facts and insights, but rephrase in their voice
4. Make it authentic - this should sound like {p_name} actually wrote it
5. Do NOT use generic corporate speak - be specific and personal
"""

    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history
    for msg in conversation_history[-6:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    try:
        # Use Claude for copy generation (better at voice matching)
        client = get_anthropic_client()
        model_id = MODEL_MAP.get(model, "claude-sonnet-4-20250514")

        response = client.messages.create(
            model=model_id,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )

        copy_text = response.content[0].text
        latency_ms = (time.time() - start_time) * 1000

        # Log the AI call
        log_ai_call(
            request_type="copy_generation",
            model=model_id,
            prompt=user_message,
            context_summary=f"Persona: {persona_name}, Platform: {platform}",
            response=copy_text,
            success=True,
            latency_ms=latency_ms,
            user_id=user_id,
            conversation_id=conversation_id
        )

        return {
            "message": copy_text,
            "copy": copy_text,
            "is_copy": True,
            "persona": persona_name,
            "platform": platform,
            "clips": []  # No video clips for copy
        }

    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        log_ai_call(
            request_type="copy_generation",
            model=model,
            prompt=user_message,
            context_summary=f"Persona: {persona_name}, Platform: {platform}",
            success=False,
            error_message=str(e),
            latency_ms=latency_ms,
            user_id=user_id,
            conversation_id=conversation_id
        )
        return {
            "message": f"Error generating copy: {str(e)}",
            "copy": None,
            "is_copy": True,
            "error": True
        }


@app.route('/chat')
def chat():
    """Chat interface for script generation."""
    # Check if user is logged in
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('chat.html')


@app.route('/ai-logs')
def ai_logs():
    """View AI call logs for quality monitoring."""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('ai_logs.html')


@app.route('/api/ai-logs', methods=['GET'])
def api_ai_logs():
    """API endpoint to fetch AI logs with filtering."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    # Get query parameters
    request_type = request.args.get('request_type')
    model = request.args.get('model')
    success = request.args.get('success')
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))

    with DatabaseSession() as db_session:
        query = db_session.query(AILog).order_by(AILog.created_at.desc())

        # Apply filters
        if request_type:
            query = query.filter(AILog.request_type == request_type)
        if model:
            query = query.filter(AILog.model.ilike(f'%{model}%'))
        if success is not None and success != '':
            query = query.filter(AILog.success == int(success))

        # Get total count for pagination
        total = query.count()

        # Apply pagination
        logs = query.offset(offset).limit(limit).all()

        return jsonify({
            'logs': [{
                'id': str(log.id),
                'request_type': log.request_type,
                'model': log.model,
                'prompt': log.prompt[:500] + '...' if log.prompt and len(log.prompt) > 500 else log.prompt,
                'context_summary': log.context_summary,
                'response': log.response[:1000] + '...' if log.response and len(log.response) > 1000 else log.response,
                'clips_generated': log.clips_generated,
                'success': log.success == 1,
                'error_message': log.error_message,
                'latency_ms': round(log.latency_ms) if log.latency_ms else None,
                'input_tokens': log.input_tokens,
                'output_tokens': log.output_tokens,
                'user_id': str(log.user_id) if log.user_id else None,
                'conversation_id': str(log.conversation_id) if log.conversation_id else None,
                'created_at': log.created_at.isoformat() if log.created_at else None
            } for log in logs],
            'total': total,
            'limit': limit,
            'offset': offset
        })


@app.route('/api/ai-logs/<log_id>', methods=['GET'])
def api_ai_log_detail(log_id):
    """Get full details of a specific AI log entry."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    with DatabaseSession() as db_session:
        log = db_session.query(AILog).filter(AILog.id == UUID(log_id)).first()

        if not log:
            return jsonify({'error': 'Log not found'}), 404

        return jsonify({
            'id': str(log.id),
            'request_type': log.request_type,
            'model': log.model,
            'prompt': log.prompt,
            'context_summary': log.context_summary,
            'response': log.response,
            'clips_generated': log.clips_generated,
            'response_json': log.response_json,
            'success': log.success == 1,
            'error_message': log.error_message,
            'latency_ms': round(log.latency_ms) if log.latency_ms else None,
            'input_tokens': log.input_tokens,
            'output_tokens': log.output_tokens,
            'user_id': str(log.user_id) if log.user_id else None,
            'conversation_id': str(log.conversation_id) if log.conversation_id else None,
            'created_at': log.created_at.isoformat() if log.created_at else None
        })


# ============================================================================
# PERSONAS MANAGEMENT
# ============================================================================

@app.route('/personas')
def personas():
    """List all personas (voice profiles)."""
    with DatabaseSession() as db_session:
        persona_list = db_session.query(Persona).filter(Persona.is_active == 1).order_by(Persona.name).all()

        # Get counts for each persona
        personas_data = []
        for p in persona_list:
            doc_count = db_session.query(Document).filter(Document.persona_id == p.id).count()
            post_count = db_session.query(SocialPost).filter(SocialPost.persona_id == p.id).count()
            video_count = db_session.query(Video).filter(Video.speaker == p.speaker_name_in_videos).count() if p.speaker_name_in_videos else 0

            personas_data.append({
                'id': str(p.id),
                'name': p.name,
                'description': p.description,
                'tone': p.tone,
                'avatar_url': p.avatar_url,
                'document_count': doc_count,
                'social_post_count': post_count,
                'video_count': video_count,
                'created_at': p.created_at
            })

    return render_template('personas.html', personas=personas_data)


@app.route('/personas/<persona_id>')
def persona_detail(persona_id):
    """View and edit a single persona."""
    with DatabaseSession() as db_session:
        persona = db_session.query(Persona).filter(Persona.id == UUID(persona_id)).first()
        if not persona:
            return "Persona not found", 404

        # Get related content
        documents = db_session.query(Document).filter(Document.persona_id == persona.id).order_by(Document.created_at.desc()).limit(20).all()
        social_posts = db_session.query(SocialPost).filter(SocialPost.persona_id == persona.id).order_by(SocialPost.posted_at.desc()).limit(20).all()
        videos = db_session.query(Video).filter(Video.speaker == persona.speaker_name_in_videos).order_by(Video.created_at.desc()).limit(20).all() if persona.speaker_name_in_videos else []

        return render_template('persona_detail.html',
                             persona=persona,
                             documents=documents,
                             social_posts=social_posts,
                             videos=videos)


@app.route('/api/personas', methods=['GET'])
def api_list_personas():
    """API: List all personas."""
    with DatabaseSession() as db_session:
        personas = db_session.query(Persona).filter(Persona.is_active == 1).order_by(Persona.name).all()
        return jsonify([{
            'id': str(p.id),
            'name': p.name,
            'description': p.description,
            'tone': p.tone,
            'speaker_name_in_videos': p.speaker_name_in_videos
        } for p in personas])


@app.route('/api/personas', methods=['POST'])
def api_create_persona():
    """API: Create a new persona."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.json or {}
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'error': 'Name is required'}), 400

    with DatabaseSession() as db_session:
        # Check if name already exists
        existing = db_session.query(Persona).filter(Persona.name == name).first()
        if existing:
            return jsonify({'error': 'A persona with this name already exists'}), 400

        persona = Persona(
            name=name,
            description=data.get('description', ''),
            tone=data.get('tone', ''),
            style_notes=data.get('style_notes', ''),
            topics=data.get('topics', []),
            vocabulary=data.get('vocabulary', []),
            speaker_name_in_videos=data.get('speaker_name_in_videos', ''),
            avatar_url=data.get('avatar_url', ''),
            created_by=UUID(session['user_id'])
        )
        db_session.add(persona)
        db_session.commit()

        return jsonify({
            'success': True,
            'id': str(persona.id),
            'name': persona.name
        })


@app.route('/api/personas/<persona_id>', methods=['PUT'])
def api_update_persona(persona_id):
    """API: Update a persona."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.json or {}

    with DatabaseSession() as db_session:
        persona = db_session.query(Persona).filter(Persona.id == UUID(persona_id)).first()
        if not persona:
            return jsonify({'error': 'Persona not found'}), 404

        # Update fields
        if 'name' in data:
            persona.name = data['name']
        if 'description' in data:
            persona.description = data['description']
        if 'tone' in data:
            persona.tone = data['tone']
        if 'style_notes' in data:
            persona.style_notes = data['style_notes']
        if 'topics' in data:
            persona.topics = data['topics']
        if 'vocabulary' in data:
            persona.vocabulary = data['vocabulary']
        if 'speaker_name_in_videos' in data:
            persona.speaker_name_in_videos = data['speaker_name_in_videos']
        if 'avatar_url' in data:
            persona.avatar_url = data['avatar_url']

        persona.updated_at = datetime.utcnow()
        db_session.commit()

        return jsonify({'success': True})


@app.route('/api/personas/<persona_id>', methods=['DELETE'])
def api_delete_persona(persona_id):
    """API: Soft delete a persona."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    with DatabaseSession() as db_session:
        persona = db_session.query(Persona).filter(Persona.id == UUID(persona_id)).first()
        if not persona:
            return jsonify({'error': 'Persona not found'}), 404

        persona.is_active = 0
        persona.updated_at = datetime.utcnow()
        db_session.commit()

        return jsonify({'success': True})


@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login page."""
    if request.method == 'POST':
        data = request.form
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')

        if not email or not password:
            return render_template('login.html', error='Email and password required')

        with DatabaseSession() as db_session:
            user = db_session.query(User).filter(User.email == email).first()
            if not user:
                return render_template('login.html', error='Invalid email or password')

            # Check password
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            if user.password_hash != password_hash:
                return render_template('login.html', error='Invalid email or password')

            if not user.is_active:
                return render_template('login.html', error='Account is disabled')

            # Check if 2FA is enabled
            if user.totp_enabled == 1 and user.totp_secret:
                # Store pending auth in session
                session['pending_2fa_user_id'] = str(user.id)
                session['pending_2fa_email'] = user.email
                return redirect(url_for('verify_2fa'))

            # No 2FA - complete login
            user.last_login = datetime.utcnow()
            db_session.commit()

            # Set up persistent session (7 days)
            session.permanent = True
            session['user_id'] = str(user.id)
            session['user_name'] = user.name
            session['user_email'] = user.email

            return redirect(url_for('chat'))

    return render_template('login.html')


@app.route('/verify-2fa', methods=['GET', 'POST'])
def verify_2fa():
    """2FA verification page."""
    if 'pending_2fa_user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        code = request.form.get('code', '').strip()

        with DatabaseSession() as db_session:
            user = db_session.query(User).filter(
                User.id == session['pending_2fa_user_id']
            ).first()

            if not user or not user.totp_secret:
                session.pop('pending_2fa_user_id', None)
                session.pop('pending_2fa_email', None)
                return redirect(url_for('login'))

            # Verify TOTP code
            totp = pyotp.TOTP(user.totp_secret)
            if totp.verify(code, valid_window=1):  # Allow 1 window tolerance
                # 2FA verified - complete login
                user.last_login = datetime.utcnow()
                db_session.commit()

                # Set up persistent session (7 days)
                session.permanent = True
                session['user_id'] = str(user.id)
                session['user_name'] = user.name
                session['user_email'] = user.email
                session.pop('pending_2fa_user_id', None)
                session.pop('pending_2fa_email', None)

                return redirect(url_for('chat'))
            else:
                return render_template('verify_2fa.html',
                    email=session.get('pending_2fa_email'),
                    error='Invalid code. Please try again.')

    return render_template('verify_2fa.html', email=session.get('pending_2fa_email'))


@app.route('/setup-2fa', methods=['GET', 'POST'])
def setup_2fa():
    """2FA setup page - requires login."""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    with DatabaseSession() as db_session:
        user = db_session.query(User).filter(User.id == session['user_id']).first()
        if not user:
            return redirect(url_for('login'))

        if request.method == 'POST':
            code = request.form.get('code', '').strip()
            secret = request.form.get('secret', '')

            # Verify the code before enabling
            totp = pyotp.TOTP(secret)
            if totp.verify(code, valid_window=1):
                user.totp_secret = secret
                user.totp_enabled = 1
                db_session.commit()
                return render_template('setup_2fa.html', success=True, user_name=user.name)
            else:
                # Regenerate QR for retry
                provisioning_uri = totp.provisioning_uri(
                    name=user.email,
                    issuer_name="MV Internal"
                )
                qr_data = generate_qr_base64(provisioning_uri)
                return render_template('setup_2fa.html',
                    secret=secret,
                    qr_code=qr_data,
                    user_name=user.name,
                    error='Invalid code. Please try again.')

        # Generate new secret
        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        provisioning_uri = totp.provisioning_uri(
            name=user.email,
            issuer_name="MV Internal"
        )

        # Generate QR code as base64
        qr_data = generate_qr_base64(provisioning_uri)

        return render_template('setup_2fa.html',
            secret=secret,
            qr_code=qr_data,
            user_name=user.name,
            already_enabled=user.totp_enabled == 1)


def generate_qr_base64(data):
    """Generate QR code as base64 data URI."""
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return 'data:image/png;base64,' + base64.b64encode(buffer.read()).decode()


@app.route('/logout')
def logout():
    """User logout."""
    session.clear()
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration page."""
    if request.method == 'POST':
        data = request.form
        name = data.get('name', '').strip()
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')
        confirm = data.get('confirm', '')

        if not name or not email or not password:
            return render_template('register.html', error='All fields are required')

        if password != confirm:
            return render_template('register.html', error='Passwords do not match')

        if len(password) < 6:
            return render_template('register.html', error='Password must be at least 6 characters')

        with DatabaseSession() as db_session:
            # Check if email exists
            existing = db_session.query(User).filter(User.email == email).first()
            if existing:
                return render_template('register.html', error='Email already registered')

            # Create user
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            user = User(
                name=name,
                email=email,
                password_hash=password_hash
            )
            db_session.add(user)
            db_session.commit()

            # Auto-login with persistent session
            session.permanent = True
            session['user_id'] = str(user.id)
            session['user_name'] = user.name
            session['user_email'] = user.email

            return redirect(url_for('chat'))

    return render_template('register.html')


# ============================================================================
# CONVERSATION API ENDPOINTS
# ============================================================================

@app.route('/api/conversations', methods=['GET'])
def api_list_conversations():
    """List all conversations for the current user."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    with DatabaseSession() as db_session:
        conversations = db_session.query(Conversation).filter(
            Conversation.user_id == UUID(session['user_id'])
        ).order_by(Conversation.updated_at.desc()).all()

        return jsonify({
            'conversations': [{
                'id': str(c.id),
                'title': c.title,
                'video_id': str(c.video_id) if c.video_id else None,
                'created_at': c.created_at.isoformat(),
                'updated_at': c.updated_at.isoformat(),
                'message_count': len(c.messages)
            } for c in conversations]
        })


@app.route('/api/conversations', methods=['POST'])
def api_create_conversation():
    """Create a new conversation."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.json or {}
    title = data.get('title', 'New Chat')
    video_id = data.get('video_id')

    with DatabaseSession() as db_session:
        conversation = Conversation(
            user_id=UUID(session['user_id']),
            title=title,
            video_id=UUID(video_id) if video_id else None
        )
        db_session.add(conversation)
        db_session.commit()

        return jsonify({
            'id': str(conversation.id),
            'title': conversation.title,
            'created_at': conversation.created_at.isoformat()
        })


@app.route('/api/conversations/<conversation_id>', methods=['GET'])
def api_get_conversation(conversation_id):
    """Get a conversation with all messages."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    with DatabaseSession() as db_session:
        conversation = db_session.query(Conversation).filter(
            Conversation.id == UUID(conversation_id),
            Conversation.user_id == UUID(session['user_id'])
        ).first()

        if not conversation:
            return jsonify({'error': 'Conversation not found'}), 404

        return jsonify({
            'id': str(conversation.id),
            'title': conversation.title,
            'video_id': str(conversation.video_id) if conversation.video_id else None,
            'created_at': conversation.created_at.isoformat(),
            'updated_at': conversation.updated_at.isoformat(),
            'messages': [{
                'id': str(m.id),
                'role': m.role,
                'content': m.content,
                'clips': m.clips_json or [],
                'model': m.model,
                'created_at': m.created_at.isoformat()
            } for m in conversation.messages]
        })


@app.route('/api/conversations/<conversation_id>', methods=['PUT'])
def api_update_conversation(conversation_id):
    """Update conversation title."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.json or {}

    with DatabaseSession() as db_session:
        conversation = db_session.query(Conversation).filter(
            Conversation.id == UUID(conversation_id),
            Conversation.user_id == UUID(session['user_id'])
        ).first()

        if not conversation:
            return jsonify({'error': 'Conversation not found'}), 404

        if 'title' in data:
            conversation.title = data['title']

        db_session.commit()

        return jsonify({'success': True})


@app.route('/api/conversations/<conversation_id>', methods=['DELETE'])
def api_delete_conversation(conversation_id):
    """Delete a conversation."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    with DatabaseSession() as db_session:
        conversation = db_session.query(Conversation).filter(
            Conversation.id == UUID(conversation_id),
            Conversation.user_id == UUID(session['user_id'])
        ).first()

        if not conversation:
            return jsonify({'error': 'Conversation not found'}), 404

        db_session.delete(conversation)
        db_session.commit()

        return jsonify({'success': True})


# ============================================================================
# COLLABORATION API ENDPOINTS
# ============================================================================

@app.route('/api/users/search', methods=['GET'])
def api_search_users():
    """Search users by name or email for inviting to conversations."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify({'users': []})

    with DatabaseSession() as db_session:
        users = db_session.query(User).filter(
            User.is_active == 1,
            User.id != UUID(session['user_id']),
            (User.name.ilike(f'%{query}%') | User.email.ilike(f'%{query}%'))
        ).limit(10).all()

        return jsonify({
            'users': [{
                'id': str(u.id),
                'name': u.name,
                'email': u.email
            } for u in users]
        })


@app.route('/api/conversations/<conversation_id>/participants', methods=['GET'])
def api_get_participants(conversation_id):
    """Get participants of a conversation."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from scripts.db import ChatParticipant

    with DatabaseSession() as db_session:
        # Check access
        conversation = db_session.query(Conversation).filter(
            Conversation.id == UUID(conversation_id)
        ).first()

        if not conversation:
            return jsonify({'error': 'Conversation not found'}), 404

        # Owner always has access
        is_owner = str(conversation.user_id) == session['user_id']
        is_participant = db_session.query(ChatParticipant).filter(
            ChatParticipant.conversation_id == UUID(conversation_id),
            ChatParticipant.user_id == UUID(session['user_id'])
        ).first() is not None

        if not is_owner and not is_participant:
            return jsonify({'error': 'Access denied'}), 403

        # Get owner
        owner = db_session.query(User).filter(User.id == conversation.user_id).first()

        # Get participants
        participants = db_session.query(ChatParticipant).filter(
            ChatParticipant.conversation_id == UUID(conversation_id)
        ).all()

        result = [{
            'id': str(owner.id) if owner else None,
            'name': owner.name if owner else 'Unknown',
            'email': owner.email if owner else '',
            'role': 'owner'
        }]

        for p in participants:
            user = db_session.query(User).filter(User.id == p.user_id).first()
            if user:
                result.append({
                    'id': str(user.id),
                    'name': user.name,
                    'email': user.email,
                    'role': p.role,
                    'joined_at': p.joined_at.isoformat() if p.joined_at else None
                })

        return jsonify({'participants': result, 'is_collaborative': conversation.is_collaborative})


@app.route('/api/conversations/<conversation_id>/invite', methods=['POST'])
def api_invite_participant(conversation_id):
    """Invite a user to collaborate on a conversation."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from scripts.db import ChatParticipant

    data = request.json or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': 'User ID required'}), 400

    with DatabaseSession() as db_session:
        # Check ownership
        conversation = db_session.query(Conversation).filter(
            Conversation.id == UUID(conversation_id),
            Conversation.user_id == UUID(session['user_id'])
        ).first()

        if not conversation:
            return jsonify({'error': 'Conversation not found or access denied'}), 404

        # Check user exists
        invitee = db_session.query(User).filter(User.id == UUID(user_id)).first()
        if not invitee:
            return jsonify({'error': 'User not found'}), 404

        # Check not already participant
        existing = db_session.query(ChatParticipant).filter(
            ChatParticipant.conversation_id == UUID(conversation_id),
            ChatParticipant.user_id == UUID(user_id)
        ).first()

        if existing:
            return jsonify({'error': 'User already a participant'}), 400

        # Add participant
        participant = ChatParticipant(
            conversation_id=UUID(conversation_id),
            user_id=UUID(user_id),
            role='member',
            invited_by=UUID(session['user_id'])
        )
        db_session.add(participant)

        # Mark conversation as collaborative
        conversation.is_collaborative = 1

        db_session.commit()

        return jsonify({
            'success': True,
            'participant': {
                'id': str(invitee.id),
                'name': invitee.name,
                'email': invitee.email,
                'role': 'member'
            }
        })


@app.route('/api/conversations/<conversation_id>/leave', methods=['POST'])
def api_leave_conversation(conversation_id):
    """Leave a conversation (for participants, not owners)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from scripts.db import ChatParticipant

    with DatabaseSession() as db_session:
        participant = db_session.query(ChatParticipant).filter(
            ChatParticipant.conversation_id == UUID(conversation_id),
            ChatParticipant.user_id == UUID(session['user_id'])
        ).first()

        if not participant:
            return jsonify({'error': 'Not a participant'}), 404

        db_session.delete(participant)
        db_session.commit()

        return jsonify({'success': True})


@app.route('/api/clips/<conversation_id>/<int:clip_index>/comments', methods=['GET'])
def api_get_clip_comments(conversation_id, clip_index):
    """Get comments on a specific clip."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from scripts.db import ClipComment

    with DatabaseSession() as db_session:
        comments = db_session.query(ClipComment).filter(
            ClipComment.conversation_id == UUID(conversation_id),
            ClipComment.clip_index == clip_index
        ).order_by(ClipComment.created_at).all()

        return jsonify({
            'comments': [{
                'id': str(c.id),
                'user_id': str(c.user_id),
                'user_name': db_session.query(User).filter(User.id == c.user_id).first().name if c.user_id else 'Unknown',
                'content': c.content,
                'mentions': c.mentions or [],
                'is_regenerate_request': c.is_regenerate_request,
                'created_at': c.created_at.isoformat()
            } for c in comments]
        })


@app.route('/api/clips/<conversation_id>/<int:clip_index>/comments', methods=['POST'])
def api_add_clip_comment(conversation_id, clip_index):
    """Add a comment on a clip. Use @mv-video to request regeneration."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from scripts.db import ClipComment

    data = request.json or {}
    content = data.get('content', '').strip()
    message_id = data.get('message_id')

    if not content:
        return jsonify({'error': 'Comment content required'}), 400

    # Parse mentions from content
    mentions = re.findall(r'@(\w+(?:-\w+)*)', content)
    is_regenerate = 'mv-video' in mentions or 'mv_video' in mentions

    with DatabaseSession() as db_session:
        comment = ClipComment(
            conversation_id=UUID(conversation_id),
            message_id=UUID(message_id) if message_id else None,
            user_id=UUID(session['user_id']),
            clip_index=clip_index,
            content=content,
            mentions=mentions,
            is_regenerate_request=1 if is_regenerate else 0
        )
        db_session.add(comment)
        db_session.commit()

        user = db_session.query(User).filter(User.id == UUID(session['user_id'])).first()

        return jsonify({
            'success': True,
            'comment': {
                'id': str(comment.id),
                'user_id': str(comment.user_id),
                'user_name': user.name if user else 'Unknown',
                'content': comment.content,
                'mentions': mentions,
                'is_regenerate_request': is_regenerate,
                'created_at': comment.created_at.isoformat()
            }
        })


@app.route('/api/clips/<conversation_id>/<int:clip_index>/regenerate', methods=['POST'])
def api_regenerate_clip(conversation_id, clip_index):
    """Regenerate a specific clip based on feedback."""
    print(f"[DEBUG] Regenerate clip called: conversation={conversation_id}, clip_index={clip_index}")

    if 'user_id' not in session:
        print("[DEBUG] Regenerate failed: Not logged in")
        return jsonify({'error': 'Not logged in'}), 401

    data = request.json or {}
    feedback = data.get('feedback', '').strip()
    original_clip = data.get('original_clip', {})
    conversation_history = data.get('history', [])
    model = data.get('model', 'claude-sonnet')

    print(f"[DEBUG] Regenerate params: feedback='{feedback}', original_clip={original_clip.get('text', '')[:50] if original_clip else 'MISSING'}, model={model}")

    if not original_clip:
        print("[DEBUG] Regenerate failed: No original clip data")
        return jsonify({'error': 'Original clip data required'}), 400

    # Build prompt for regeneration
    regenerate_prompt = f"""The user wants to find a DIFFERENT clip to replace this one.

ORIGINAL CLIP (to replace):
- Video: {original_clip.get('video_title', 'Unknown')}
- Time: {original_clip.get('start_time', 0):.1f}s - {original_clip.get('end_time', 0):.1f}s
- Text: "{original_clip.get('text', '')}"

USER FEEDBACK: {feedback if feedback else 'Find a better alternative'}

Find ONE alternative clip that:
1. Covers a similar topic/theme but with different content
2. Is from a DIFFERENT part of the video OR a different video entirely
3. Better matches what the user is looking for

Output format:
```json
{{"clips": [{{"video_id": "...", "video_title": "...", "start_time": 0.0, "end_time": 0.0, "text": "..."}}]}}
```"""

    # Search for relevant context
    search_query = feedback or original_clip.get('text', '')[:100]
    print(f"[DEBUG] Regenerate searching with query: '{search_query[:80]}...'")
    context = search_transcripts_for_context(search_query)

    if not context:
        print("[DEBUG] Regenerate failed: No context found from search")
        return jsonify({'error': 'No relevant content found'}), 404

    print(f"[DEBUG] Regenerate found {len(context)} context segments, generating with AI...")

    # Generate with AI
    result = generate_script_with_ai(
        regenerate_prompt, context, conversation_history, model=model,
        user_id=session.get('user_id'), conversation_id=conversation_id
    )

    print(f"[DEBUG] Regenerate AI result: clips={len(result.get('clips', []))}, message={result.get('message', '')[:100] if result.get('message') else 'none'}")

    if result.get('clips'):
        return jsonify({
            'success': True,
            'alternative_clips': result['clips'],
            'message': result.get('message', '')
        })
    else:
        print("[DEBUG] Regenerate failed: AI returned no clips")
        return jsonify({
            'success': False,
            'error': 'Could not find alternative clips',
            'message': result.get('message', '')
        })


@app.route('/api/records/<conversation_id>/<int:record_index>/comments', methods=['POST'])
def api_add_record_comment(conversation_id, record_index):
    """Add a comment on a record/narration section. Use @mv-video to request regeneration."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from scripts.db import ClipComment

    data = request.json or {}
    content = data.get('content', '').strip()

    if not content:
        return jsonify({'error': 'Comment content required'}), 400

    # Parse mentions from content
    mentions = re.findall(r'@(\w+(?:-\w+)*)', content)
    is_regenerate = 'mv-video' in mentions or 'mv_video' in mentions

    with DatabaseSession() as db_session:
        # Reuse ClipComment table with negative index to indicate record sections
        comment = ClipComment(
            conversation_id=UUID(conversation_id),
            user_id=UUID(session['user_id']),
            clip_index=-1 - record_index,  # Use negative indices for records
            content=content,
            mentions=mentions,
            is_regenerate_request=1 if is_regenerate else 0
        )
        db_session.add(comment)
        db_session.commit()

        user = db_session.query(User).filter(User.id == UUID(session['user_id'])).first()

        return jsonify({
            'success': True,
            'comment': {
                'id': str(comment.id),
                'user_id': str(comment.user_id),
                'user_name': user.name if user else 'Unknown',
                'content': comment.content,
                'mentions': mentions,
                'is_regenerate_request': is_regenerate,
                'created_at': comment.created_at.isoformat()
            }
        })


@app.route('/api/records/<conversation_id>/<int:record_index>/regenerate', methods=['POST'])
def api_regenerate_record(conversation_id, record_index):
    """Regenerate a narration/record section based on feedback."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.json or {}
    feedback = data.get('feedback', '').strip()
    original_text = data.get('original_text', '').strip()
    conversation_history = data.get('history', [])
    model = data.get('model', 'claude-sonnet')

    if not original_text:
        return jsonify({'error': 'Original text required'}), 400

    # Build prompt for regeneration
    regenerate_prompt = f"""The user wants to regenerate this narration/voiceover text.

ORIGINAL NARRATION (to rewrite):
"{original_text}"

USER FEEDBACK: {feedback if feedback else 'Provide an alternative version'}

Write ONE alternative narration that:
1. Conveys a similar message but with different wording
2. Maintains a professional, engaging tone suitable for voiceover
3. Is approximately the same length as the original
4. Better matches what the user is looking for based on their feedback

Output ONLY the new narration text, nothing else. Do not include quotes or any other formatting."""

    # Generate with AI
    start_time = time.time()
    actual_model = "claude-sonnet-4-20250514"

    try:
        client = get_anthropic_client()

        response = client.messages.create(
            model=actual_model,
            max_tokens=500,
            messages=[
                {"role": "user", "content": regenerate_prompt}
            ]
        )

        new_text = response.content[0].text.strip()
        # Remove any quotes that might have been added
        new_text = new_text.strip('"\'')

        # Log successful AI call
        latency_ms = (time.time() - start_time) * 1000
        log_ai_call(
            request_type="regenerate_record",
            model=actual_model,
            prompt=regenerate_prompt,
            response=new_text,
            success=True,
            latency_ms=latency_ms,
            input_tokens=response.usage.input_tokens if hasattr(response, 'usage') else None,
            output_tokens=response.usage.output_tokens if hasattr(response, 'usage') else None,
            user_id=session.get('user_id'),
            conversation_id=conversation_id
        )

        return jsonify({
            'success': True,
            'new_text': new_text
        })
    except Exception as e:
        # Log failed AI call
        latency_ms = (time.time() - start_time) * 1000
        log_ai_call(
            request_type="regenerate_record",
            model=actual_model,
            prompt=regenerate_prompt,
            success=False,
            error_message=str(e),
            latency_ms=latency_ms,
            user_id=session.get('user_id'),
            conversation_id=conversation_id
        )

        print(f"Error regenerating record: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/chat', methods=['POST'])
def api_chat():
    """Handle chat messages for script generation or copy generation."""
    try:
        data = request.json
        user_message = data.get('message', '').strip()
        conversation_history = data.get('history', [])
        conversation_id = data.get('conversation_id')
        model = data.get('model', 'gpt-4o')  # Default to GPT-4o
        previous_clips = data.get('previous_clips', [])  # Clips from previous scripts to exclude

        if not user_message:
            return jsonify({'error': 'No message provided'}), 400

        # Detect if user wants copy generation vs video scripts
        intent = detect_copy_intent(user_message)

        # Search for relevant transcript context (used for both modes)
        context = search_transcripts_for_context(user_message)

        # Also search audio recordings for relevant segments
        audio_context = search_audio_for_context(user_message, limit=50)

        # COPY GENERATION MODE
        if intent['is_copy'] and intent['persona_name']:
            # Use Claude for copy generation (better at voice matching)
            copy_model = 'claude-sonnet' if not model.startswith('claude') else model

            result = generate_copy_with_ai(
                user_message=user_message,
                persona_name=intent['persona_name'],
                platform=intent['platform'] or 'general',
                transcript_context=context or [],
                conversation_history=conversation_history,
                model=copy_model,
                user_id=session.get('user_id'),
                conversation_id=conversation_id
            )

            # Save messages to conversation
            if conversation_id and 'user_id' in session:
                with DatabaseSession() as db_session:
                    user_msg = ChatMessage(
                        conversation_id=UUID(conversation_id),
                        role='user',
                        content=user_message
                    )
                    db_session.add(user_msg)

                    assistant_msg = ChatMessage(
                        conversation_id=UUID(conversation_id),
                        role='assistant',
                        content=result['message'],
                        model=copy_model
                    )
                    db_session.add(assistant_msg)

                    conv = db_session.query(Conversation).filter(Conversation.id == UUID(conversation_id)).first()
                    if conv:
                        conv.updated_at = datetime.utcnow()
                    db_session.commit()

            return jsonify({
                'response': result['message'],
                'clips': [],
                'has_script': False,
                'is_copy': True,
                'persona': intent['persona_name'],
                'platform': intent['platform'],
                'context_segments': len(context) if context else 0,
                'model': copy_model,
                'conversation_id': conversation_id
            })

        # VIDEO SCRIPT MODE (default)

        if not context:
            response_text = "I couldn't find any matching content in the video library. Try different keywords or check the Transcripts page to see what's available."

            # Save messages if conversation exists
            if conversation_id and 'user_id' in session:
                with DatabaseSession() as db_session:
                    # Save user message
                    user_msg = ChatMessage(
                        conversation_id=UUID(conversation_id),
                        role='user',
                        content=user_message
                    )
                    db_session.add(user_msg)

                    # Save assistant response
                    assistant_msg = ChatMessage(
                        conversation_id=UUID(conversation_id),
                        role='assistant',
                        content=response_text,
                        model=model
                    )
                    db_session.add(assistant_msg)

                    # Update conversation timestamp
                    conv = db_session.query(Conversation).filter(Conversation.id == UUID(conversation_id)).first()
                    if conv:
                        conv.updated_at = datetime.utcnow()

                    db_session.commit()

            return jsonify({
                'response': response_text,
                'clips': [],
                'has_script': False,
                'context_segments': 0,
                'model': model,
                'conversation_id': conversation_id
            })

        # Generate response with AI (exclude previously used clips)
        result = generate_script_with_ai(
            user_message, context, conversation_history, model=model, exclude_clips=previous_clips,
            user_id=session.get('user_id'), conversation_id=conversation_id
        )

        # Save messages to conversation
        if conversation_id and 'user_id' in session:
            with DatabaseSession() as db_session:
                # Save user message
                user_msg = ChatMessage(
                    conversation_id=UUID(conversation_id),
                    role='user',
                    content=user_message
                )
                db_session.add(user_msg)

                # Save assistant response with clips
                assistant_msg = ChatMessage(
                    conversation_id=UUID(conversation_id),
                    role='assistant',
                    content=result['message'],
                    clips_json=result.get('clips', []),
                    model=model
                )
                db_session.add(assistant_msg)

                # Update conversation timestamp and title if it's the first message
                conv = db_session.query(Conversation).filter(Conversation.id == UUID(conversation_id)).first()
                if conv:
                    conv.updated_at = datetime.utcnow()
                    # Auto-title based on first user message if still default
                    if conv.title == 'New Chat':
                        conv.title = user_message[:50] + ('...' if len(user_message) > 50 else '')

                db_session.commit()

        return jsonify({
            'response': result['message'],
            'clips': result.get('clips', []),
            'audio_clips': audio_context[:20] if audio_context else [],  # Include relevant audio segments
            'has_script': result.get('has_script', False),
            'context_segments': len(context),
            'audio_segments': len(audio_context) if audio_context else 0,
            'model': model,
            'conversation_id': conversation_id
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': f'Server error: {str(e)}',
            'response': f'Sorry, there was an error processing your request: {str(e)}',
            'clips': [],
            'has_script': False
        }), 500


@app.route('/api/script-feedback', methods=['POST'])
def api_script_feedback():
    """Save user feedback on a generated script."""
    try:
        data = request.json
        query = data.get('query', '')
        script = data.get('script', '')
        clips = data.get('clips', [])
        rating = data.get('rating', 0)  # 1 = good, -1 = bad
        model = data.get('model', 'unknown')

        if not script or rating == 0:
            return jsonify({'error': 'Script and rating required'}), 400

        with DatabaseSession() as db_session:
            feedback = ScriptFeedback(
                query=query,
                script=script,
                clips_json=clips,
                rating=rating,
                model=model
            )
            db_session.add(feedback)
            db_session.commit()

            return jsonify({
                'success': True,
                'id': str(feedback.id),
                'message': 'Thanks! This helps improve future scripts.'
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/script-examples', methods=['GET'])
def api_script_examples():
    """Get good script examples for few-shot learning."""
    try:
        with DatabaseSession() as db_session:
            # Get top-rated scripts (rating = 1)
            examples = db_session.query(ScriptFeedback).filter(
                ScriptFeedback.rating == 1
            ).order_by(ScriptFeedback.created_at.desc()).limit(5).all()

            return jsonify({
                'examples': [{
                    'id': str(e.id),
                    'query': e.query,
                    'script': e.script,
                    'clips': e.clips_json,
                    'model': e.model
                } for e in examples]
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/video-preview/<video_id>')
def api_video_preview(video_id):
    """Get presigned S3 URL for video preview."""
    try:
        with DatabaseSession() as db_session:
            video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            if not video:
                return jsonify({'error': 'Video not found'}), 404

            if not video.s3_key:
                return jsonify({'error': 'Video not in S3'}), 404

            # Generate presigned URL (valid for 1 hour)
            config = get_config()
            bucket = config.s3_bucket

            s3_client = get_s3_client()
            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket, 'Key': video.s3_key},
                ExpiresIn=3600  # 1 hour
            )

            return jsonify({
                'url': presigned_url,
                'video_id': str(video.id),
                'filename': video.filename,
                'duration': video.duration_seconds
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/audio-preview/<audio_id>')
def api_audio_preview(audio_id):
    """Get presigned S3 URL for audio preview."""
    try:
        with DatabaseSession() as db_session:
            audio = db_session.query(AudioRecording).filter(AudioRecording.id == UUID(audio_id)).first()
            if not audio:
                return jsonify({'error': 'Audio not found'}), 404

            if not audio.s3_key:
                return jsonify({'error': 'Audio not in S3'}), 404

            # Generate presigned URL (valid for 1 hour)
            s3_client = get_s3_client()
            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': audio.s3_bucket, 'Key': audio.s3_key},
                ExpiresIn=3600  # 1 hour
            )

            return jsonify({
                'url': presigned_url,
                'audio_id': str(audio.id),
                'title': audio.title,
                'filename': audio.filename,
                'duration': float(audio.duration_seconds) if audio.duration_seconds else None
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/audio-clip/<audio_id>')
def api_audio_clip(audio_id):
    """Get audio clip info with presigned URL and time range for playback."""
    try:
        start_time = request.args.get('start', type=float, default=0)
        end_time = request.args.get('end', type=float, default=None)

        with DatabaseSession() as db_session:
            audio = db_session.query(AudioRecording).filter(AudioRecording.id == UUID(audio_id)).first()
            if not audio:
                return jsonify({'error': 'Audio not found'}), 404

            if not audio.s3_key:
                return jsonify({'error': 'Audio not in S3'}), 404

            # Generate presigned URL
            s3_client = get_s3_client()
            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': audio.s3_bucket, 'Key': audio.s3_key},
                ExpiresIn=3600
            )

            return jsonify({
                'url': presigned_url,
                'audio_id': str(audio.id),
                'title': audio.title,
                'start_time': start_time,
                'end_time': end_time or (float(audio.duration_seconds) if audio.duration_seconds else None),
                'duration': float(audio.duration_seconds) if audio.duration_seconds else None
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/chat/create-video', methods=['POST'])
def api_create_video_from_chat():
    """Create video clips from chat-generated script."""
    data = request.json
    clips = data.get('clips', [])
    title = data.get('title', 'Chat Script')

    if not clips:
        return jsonify({'error': 'No clips provided'}), 400

    # Create clips folder
    safe_title = re.sub(r'[^\w\-_ ]', '_', title)
    output_dir = Path('local_clips') / safe_title
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate edit script
    script_content = f"# {title}\n"
    script_content += f"# Generated from Chat\n"
    script_content += f"# Total clips: {len(clips)}\n\n"

    total_duration = 0
    for i, clip in enumerate(clips, 1):
        duration = clip.get('duration', clip.get('end_time', 0) - clip.get('start_time', 0))
        total_duration += duration
        script_content += f"## Clip {i}\n"
        script_content += f"Source: {clip.get('video_title', 'Unknown')}\n"
        script_content += f"Time: {clip.get('start_time', 0):.1f}s - {clip.get('end_time', 0):.1f}s ({duration:.1f}s)\n"
        script_content += f"Text: \"{clip.get('text', '')}\"\n"
        script_content += f"Verified: {'Yes' if clip.get('verified') else 'No'}\n\n"

    script_content += f"\n# Total Duration: {total_duration:.1f}s\n"

    with open(output_dir / 'EDIT_SCRIPT.txt', 'w') as f:
        f.write(script_content)

    return jsonify({
        'success': True,
        'folder': str(output_dir),
        'script_path': str(output_dir / 'EDIT_SCRIPT.txt'),
        'total_duration': total_duration
    })


@app.route('/api/chat/export-script', methods=['POST'])
def api_export_script():
    """Generate a downloadable script file."""
    from flask import Response

    data = request.json
    clips = data.get('clips', [])
    title = data.get('title', 'Video Script')
    script_text = data.get('script_text', '')

    if not clips:
        return jsonify({'error': 'No clips provided'}), 400

    # Generate edit script content
    content = f"# {title}\n"
    content += f"# Generated from MV Videos\n"
    content += f"# Total clips: {len(clips)}\n"
    content += "=" * 50 + "\n\n"

    if script_text:
        # Clean up the script text - remove JSON blocks
        clean_script = re.sub(r'```json[\s\S]*?```', '', script_text)
        clean_script = re.sub(r'---', '', clean_script)
        content += "SCRIPT:\n\n"
        content += clean_script.strip()
        content += "\n\n" + "=" * 50 + "\n\n"

    content += "CLIP DETAILS:\n\n"
    total_duration = 0
    for i, clip in enumerate(clips, 1):
        duration = clip.get('duration', clip.get('end_time', 0) - clip.get('start_time', 0))
        total_duration += duration
        content += f"--- Clip {i} ---\n"
        content += f"Source: {clip.get('video_title', 'Unknown')}\n"
        content += f"Speaker: {clip.get('speaker', 'Unknown')}\n"
        content += f"Time: {clip.get('start_time', 0):.1f}s - {clip.get('end_time', 0):.1f}s ({duration:.1f}s)\n"
        content += f"Text: \"{clip.get('text', '')}\"\n"
        content += f"Video ID: {clip.get('video_id', 'Unknown')}\n"
        content += f"Verified: {'Yes' if clip.get('verified') else 'No'}\n\n"

    content += "=" * 50 + "\n"
    content += f"Total Duration: {total_duration:.1f}s\n"

    # Create filename
    safe_title = re.sub(r'[^\w\-_ ]', '_', title)
    filename = f"{safe_title}_script.txt"

    return Response(
        content,
        mimetype='text/plain',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"'
        }
    )


@app.route('/api/video-thumbnail/<video_id>')
def api_video_thumbnail(video_id):
    """Return pre-generated thumbnail from S3 (fast) or redirect to placeholder."""
    try:
        with DatabaseSession() as db_session:
            video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            if not video:
                return '', 404

            config = get_config()
            bucket = config.s3_bucket
            s3_client = get_s3_client()

            # If we have a pre-generated thumbnail, return presigned URL redirect
            if video.thumbnail_s3_key:
                presigned_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': bucket, 'Key': video.thumbnail_s3_key},
                    ExpiresIn=3600
                )
                return redirect(presigned_url)

            # No pre-generated thumbnail - return 404 (placeholder will show)
            return '', 404

    except Exception as e:
        print(f"[THUMB] Error: {e}")
        return '', 404


@app.route('/api/clip-preview/<video_id>')
def api_clip_preview(video_id):
    """Return presigned URL for streaming preview - fast and reliable."""
    start_time = request.args.get('start', type=float, default=0)
    end_time = request.args.get('end', type=float, default=30)

    try:
        with DatabaseSession() as db_session:
            video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            if not video:
                return jsonify({'error': 'Video not found'}), 404

            if not video.s3_key:
                return jsonify({'error': 'Video not in S3'}), 404

            config = get_config()
            bucket = config.s3_bucket
            s3_client = get_s3_client()

            # Generate presigned URL for streaming - browser will handle seeking
            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket, 'Key': video.s3_key},
                ExpiresIn=3600
            )

            return jsonify({
                'url': presigned_url,
                'start': start_time,
                'end': end_time,
                'title': video.filename or video.original_filename
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/clip-download/<video_id>')
def api_clip_download(video_id):
    """Download a clip at original quality."""
    import subprocess
    import shutil

    start_time = request.args.get('start', type=float, default=0)
    end_time = request.args.get('end', type=float, default=30)
    duration = end_time - start_time

    try:
        with DatabaseSession() as db_session:
            video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            if not video:
                return jsonify({'error': 'Video not found'}), 404

            if not video.s3_key:
                return jsonify({'error': 'Video not in S3'}), 404

            config = get_config()
            bucket = config.s3_bucket
            s3_client = get_s3_client()

            # Use home directory for cache (more space than /tmp)
            cache_base = Path('/home/ec2-user/video_cache')
            cache_base.mkdir(exist_ok=True)

            # Download video to local cache first (ffmpeg crashes with S3 URLs)
            videos_cache = cache_base / 'videos'
            videos_cache.mkdir(exist_ok=True)
            cached_video = videos_cache / f"{video_id}.mp4"

            # Download if not cached or file is too small (incomplete download)
            if not cached_video.exists() or cached_video.stat().st_size < 1000:
                print(f"[DOWNLOAD] Downloading video {video_id}...")
                import urllib.request
                presigned_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': bucket, 'Key': video.s3_key},
                    ExpiresIn=3600
                )
                urllib.request.urlretrieve(presigned_url, str(cached_video))
                print(f"[DOWNLOAD] Downloaded {cached_video.stat().st_size} bytes")

            # Output clips directory
            clips_dir = cache_base / 'clips'
            clips_dir.mkdir(exist_ok=True)

            # Create output file
            safe_name = re.sub(r'[^\w\-_ ]', '_', video.filename or 'clip')
            safe_name = safe_name.replace('.mp4', '')
            filename = f"{safe_name}_{start_time:.0f}s-{end_time:.0f}s.mp4"
            output_path = clips_dir / f"{video_id}_{start_time:.0f}_{end_time:.0f}.mp4"

            # Clean up old clips to save space (keep only last 50)
            try:
                existing_clips = sorted(clips_dir.glob('*.mp4'), key=lambda x: x.stat().st_mtime)
                if len(existing_clips) > 50:
                    for old_clip in existing_clips[:-50]:
                        old_clip.unlink()
            except:
                pass

            ffmpeg_path = shutil.which('ffmpeg') or '/usr/local/bin/ffmpeg'

            # Extract clip from local file with QuickTime-compatible encoding
            print(f"[DOWNLOAD] Extracting clip from {start_time}s to {end_time}s")
            cmd = [
                ffmpeg_path, '-y',
                '-ss', str(start_time),
                '-i', str(cached_video),
                '-t', str(duration),
                '-c:v', 'libx264',
                '-profile:v', 'high',      # High profile for quality
                '-level', '4.0',            # Level 4.0 for broad compatibility
                '-pix_fmt', 'yuv420p',      # Required for QuickTime
                '-preset', 'fast',
                '-crf', '18',               # High quality
                '-c:a', 'aac',
                '-b:a', '192k',
                '-ar', '48000',             # Standard audio sample rate
                '-movflags', '+faststart',  # Enable streaming
                str(output_path)
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=300)

            if result.returncode != 0:
                error_msg = result.stderr.decode()[:500]
                print(f"[DOWNLOAD] Encoding failed: {error_msg}")
                return jsonify({'error': 'Failed to extract clip', 'details': error_msg}), 500

            if not output_path.exists() or output_path.stat().st_size == 0:
                return jsonify({'error': 'Output file is empty or missing'}), 500

            print(f"[DOWNLOAD] Success: {output_path} ({output_path.stat().st_size} bytes)")

            return send_file(
                str(output_path),
                mimetype='video/mp4',
                as_attachment=True,
                download_name=filename
            )

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Download timed out - clip may be too long'}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/video-download/<video_id>')
def api_video_download(video_id):
    """Download the full video."""
    try:
        with DatabaseSession() as db_session:
            video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            if not video:
                return jsonify({'error': 'Video not found'}), 404

            if not video.s3_key:
                return jsonify({'error': 'Video not in S3'}), 404

            config = get_config()
            bucket = config.s3_bucket
            s3_client = get_s3_client()

            # Generate presigned URL for direct download
            url = s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': bucket,
                    'Key': video.s3_key,
                    'ResponseContentDisposition': f'attachment; filename="{video.filename}"'
                },
                ExpiresIn=3600
            )

            return redirect(url)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/transcript-segment/update', methods=['POST'])
def api_update_transcript_segment():
    """Update transcript text for a specific segment by time range."""
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        video_id = data.get('video_id')
        start_time = data.get('start_time')
        end_time = data.get('end_time')
        new_text = data.get('text')

        if not all([video_id, start_time is not None, end_time is not None, new_text]):
            return jsonify({'error': 'Missing required fields'}), 400

        with DatabaseSession() as db_session:
            # Find transcript for this video
            from scripts.db import Transcript, TranscriptSegment
            transcript = db_session.query(Transcript).filter(
                Transcript.video_id == UUID(video_id)
            ).first()

            if not transcript:
                return jsonify({'error': 'Transcript not found'}), 404

            # Find segments that overlap with the given time range
            segments = db_session.query(TranscriptSegment).filter(
                TranscriptSegment.transcript_id == transcript.id,
                TranscriptSegment.start_time >= float(start_time) - 0.5,
                TranscriptSegment.end_time <= float(end_time) + 0.5
            ).order_by(TranscriptSegment.start_time).all()

            if not segments:
                # Try a broader search
                segments = db_session.query(TranscriptSegment).filter(
                    TranscriptSegment.transcript_id == transcript.id,
                    TranscriptSegment.start_time >= float(start_time) - 2,
                    TranscriptSegment.start_time <= float(end_time) + 2
                ).order_by(TranscriptSegment.start_time).all()

            if not segments:
                return jsonify({'error': 'No matching segments found'}), 404

            # If multiple segments match, merge them into one with the new text
            if len(segments) > 1:
                # First segment gets the new text and expands to cover all time
                segments[0].text = new_text.strip()
                segments[0].end_time = segments[-1].end_time

                # Delete the other segments
                for seg in segments[1:]:
                    db_session.delete(seg)
            else:
                # Single segment - just update the text
                segments[0].text = new_text.strip()

            # Rebuild full_text from all segments
            all_segments = db_session.query(TranscriptSegment).filter(
                TranscriptSegment.transcript_id == transcript.id
            ).order_by(TranscriptSegment.start_time).all()

            transcript.full_text = ' '.join(seg.text for seg in all_segments if seg.text)
            transcript.word_count = len(transcript.full_text.split())

            db_session.commit()

            return jsonify({
                'success': True,
                'message': f'Updated segment and rebuilt transcript',
                'segment_id': str(segments[0].id)
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/transcripts/<transcript_id>/identify-speakers', methods=['POST'])
def api_identify_speakers(transcript_id):
    """Use AI to identify and label speakers in a transcript."""
    try:
        data = request.json or {}
        known_speakers = data.get('known_speakers', [])  # List of known speaker names

        with DatabaseSession() as db_session:
            transcript = db_session.query(Transcript).filter(
                Transcript.id == UUID(transcript_id)
            ).first()

            if not transcript:
                return jsonify({'error': 'Transcript not found'}), 404

            # Get video info
            video = db_session.query(Video).filter(Video.id == transcript.video_id).first()
            video_speaker = video.speaker if video else None

            # Get segments
            segments = db_session.query(TranscriptSegment).filter(
                TranscriptSegment.transcript_id == transcript.id
            ).order_by(TranscriptSegment.start_time).limit(100).all()

            if not segments:
                return jsonify({'error': 'No segments found'}), 404

            # Build transcript with timestamps
            transcript_text = ""
            for i, seg in enumerate(segments):
                mins = int(seg.start_time // 60)
                secs = int(seg.start_time % 60)
                transcript_text += f"[{mins}:{secs:02d}] {seg.text}\n"

            # Add known speaker context
            speaker_context = ""
            if video_speaker:
                speaker_context = f"The main speaker is: {video_speaker}\n"
            if known_speakers:
                speaker_context += f"Other known speakers: {', '.join(known_speakers)}\n"

            # Use Claude to identify speakers
            client = get_anthropic_client()

            prompt = f"""Analyze this transcript and identify different speakers. Look for:
- Changes in speaking style or tone
- Questions followed by answers
- Introductions or speaker identifications
- Different perspectives or topics

{speaker_context}

TRANSCRIPT:
{transcript_text[:8000]}

For each distinct voice/speaker you identify, provide:
1. A label (use actual name if mentioned, otherwise "Speaker 1", "Speaker 2", etc.)
2. Time ranges where they speak (approximate)
3. Key characteristics that helped identify them

Respond in JSON format:
{{
  "speakers": [
    {{
      "label": "Speaker Name or Speaker 1",
      "time_ranges": ["0:00-1:30", "3:45-5:00"],
      "characteristics": "Main presenter, discusses NASA"
    }}
  ],
  "speaker_changes": [
    {{"time": "0:00", "speaker": "Speaker 1"}},
    {{"time": "1:30", "speaker": "Speaker 2"}}
  ]
}}"""

            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )

            response_text = response.content[0].text

            # Parse JSON from response
            import re
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                result = json.loads(json_match.group())
            else:
                result = {"speakers": [], "speaker_changes": []}

            return jsonify({
                'success': True,
                'speakers': result.get('speakers', []),
                'speaker_changes': result.get('speaker_changes', [])
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/segments/<segment_id>/speaker', methods=['PUT'])
def api_update_segment_speaker(segment_id):
    """Update speaker label for a segment."""
    try:
        data = request.json
        speaker = data.get('speaker', '').strip()

        with DatabaseSession() as db_session:
            segment = db_session.query(TranscriptSegment).filter(
                TranscriptSegment.id == UUID(segment_id)
            ).first()

            if not segment:
                return jsonify({'error': 'Segment not found'}), 404

            segment.speaker = speaker if speaker else None
            db_session.commit()

            return jsonify({'success': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/videos/<video_id>/autofill', methods=['POST'])
def api_autofill_video(video_id):
    """Use AI to auto-fill video metadata from transcript."""
    try:
        with DatabaseSession() as db_session:
            video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            if not video:
                return jsonify({'error': 'Video not found'}), 404

            # Get transcript
            transcript = db_session.query(Transcript).filter(
                Transcript.video_id == video.id,
                Transcript.status == 'completed'
            ).first()

            if not transcript:
                return jsonify({'error': 'No transcript available for this video'}), 404

            # Get transcript segments
            segments = db_session.query(TranscriptSegment).filter(
                TranscriptSegment.transcript_id == transcript.id
            ).order_by(TranscriptSegment.start_time).all()

            if not segments:
                return jsonify({'error': 'Transcript has no segments'}), 404

            # Build transcript text (first 5000 chars for context)
            transcript_text = ' '.join(s.text for s in segments)[:5000]

            # Get existing video metadata for context
            existing_speaker = video.speaker or ''
            filename = video.filename or ''

            # Use Claude to analyze
            client = get_anthropic_client()

            prompt = f"""Analyze this video transcript and extract metadata. The video filename is "{filename}".

TRANSCRIPT (first part):
{transcript_text}

Based on this transcript, provide:
1. A 1-2 sentence description of what this video is about
2. The speaker's name (if mentioned or identifiable). Current value: "{existing_speaker}"
3. The event name (conference, show, interview, etc.) if mentioned
4. 3-5 relevant topic tags (comma-separated)
5. Any other speakers mentioned (comma-separated)

Respond in this exact JSON format:
{{
  "description": "...",
  "speaker": "...",
  "event_name": "...",
  "topics": "...",
  "other_speakers": "..."
}}

If you can't determine a field, use empty string. Be concise."""

            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )

            # Parse response
            response_text = response.content[0].text

            # Extract JSON from response
            import re
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                suggestions = json.loads(json_match.group())
            else:
                suggestions = {}

            return jsonify({
                'success': True,
                'suggestions': suggestions,
                'transcript_preview': transcript_text[:500] + '...'
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/videos/<video_id>', methods=['PUT'])
def api_update_video(video_id):
    """Update video metadata."""
    try:
        data = request.json

        with DatabaseSession() as db_session:
            video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            if not video:
                return jsonify({'error': 'Video not found'}), 404

            # Update allowed fields
            if 'filename' in data:
                # Preserve .mp4 extension if not provided
                new_filename = data['filename']
                if new_filename and not new_filename.endswith('.mp4'):
                    new_filename = new_filename + '.mp4'
                video.filename = new_filename
            if 'speaker' in data:
                video.speaker = data['speaker'] or None
            if 'event_name' in data:
                video.event_name = data['event_name'] or None
            if 'event_date' in data:
                if data['event_date']:
                    from datetime import datetime as dt
                    video.event_date = dt.strptime(data['event_date'], '%Y-%m-%d').date()
                else:
                    video.event_date = None
            if 'description' in data:
                video.description = data['description'] or None

            # Handle custom fields via extra_data (JSONB)
            if 'tags' in data:
                extra = video.extra_data or {}
                extra['tags'] = data['tags']
                video.extra_data = extra
            if 'topics' in data:
                extra = video.extra_data or {}
                extra['topics'] = data['topics']
                video.extra_data = extra
            if 'custom_fields' in data:
                extra = video.extra_data or {}
                extra.update(data['custom_fields'])
                video.extra_data = extra

            db_session.commit()

            return jsonify({
                'success': True,
                'video_id': str(video.id),
                'message': 'Video updated successfully'
            })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/videos/<video_id>', methods=['GET'])
def api_get_video(video_id):
    """Get single video details."""
    try:
        with DatabaseSession() as db_session:
            video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            if not video:
                return jsonify({'error': 'Video not found'}), 404

            return jsonify({
                'id': str(video.id),
                'filename': video.filename,
                'speaker': video.speaker,
                'event_name': video.event_name,
                'event_date': video.event_date.strftime('%Y-%m-%d') if video.event_date else None,
                'description': video.description,
                'duration': float(video.duration_seconds) if video.duration_seconds else None,
                'extra_data': video.extra_data or {},
                'tags': (video.extra_data or {}).get('tags', ''),
                'topics': (video.extra_data or {}).get('topics', ''),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)

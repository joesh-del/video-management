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

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.db import DatabaseSession, Video, Transcript, TranscriptSegment, CompiledVideo
from scripts.storylines import get_cached_storylines, generate_storylines, Storyline
from scripts.config_loader import get_config

app = Flask(__name__)
app.secret_key = os.urandom(24)


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
    with DatabaseSession() as session:
        video_count = session.query(Video).count()
        transcript_count = session.query(Transcript).filter(Transcript.status == "completed").count()

    storylines = get_cached_storylines() or []

    return render_template('index.html',
                         video_count=video_count,
                         transcript_count=transcript_count,
                         storyline_count=len(storylines))


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

        segment_list = [{
            'start': float(s.start_time),
            'end': float(s.end_time),
            'text': s.text,
        } for s in segments]

        return render_template('transcript_detail.html',
                             transcript_id=transcript_id,
                             video_title=video.filename if video else "Unknown",
                             segments=segment_list)


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


@app.route('/storylines')
def storylines():
    """List all generated storylines."""
    storyline_list = get_cached_storylines() or []
    return render_template('storylines.html', storylines=storyline_list)


@app.route('/storylines/<int:storyline_id>')
def storyline_detail(storyline_id):
    """View a single storyline with clips."""
    storyline_list = get_cached_storylines() or []
    storyline = None
    for s in storyline_list:
        if s.id == storyline_id:
            storyline = s
            break

    if not storyline:
        return "Storyline not found", 404

    return render_template('storyline_detail.html', storyline=storyline)


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


@app.route('/api/storylines/generate', methods=['POST'])
def api_generate_storylines():
    """Generate new storylines via API."""
    try:
        storylines = generate_storylines(force_refresh=True)
        return jsonify({'success': True, 'count': len(storylines)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


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


def search_transcripts_for_context(query: str, limit: int = 500):
    """Search transcripts for relevant segments based on query keywords."""
    # Extract keywords from query
    keywords = re.findall(r'\b\w{4,}\b', query.lower())

    results = []
    with DatabaseSession() as db_session:
        # Get all videos with their transcripts
        videos = db_session.query(Video).all()
        video_map = {str(v.id): {
            'filename': v.filename,
            'speaker': v.speaker or 'Unknown',
            'event_name': v.event_name
        } for v in videos}

        # Search for matching segments
        for keyword in keywords[:5]:  # Limit keywords to search
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
                if transcript:
                    video_info = video_map.get(str(transcript.video_id), {})
                    results.append({
                        'video_id': str(transcript.video_id),
                        'video_title': video_info.get('filename', 'Unknown'),
                        'speaker': video_info.get('speaker', 'Unknown'),
                        'start': float(seg.start_time),
                        'end': float(seg.end_time),
                        'text': seg.text,
                        'segment_id': str(seg.id)
                    })

        # If no keyword matches, get a sample of recent transcripts
        if not results:
            transcripts = db_session.query(Transcript).filter(
                Transcript.status == 'completed'
            ).limit(20).all()

            for t in transcripts:
                video_info = video_map.get(str(t.video_id), {})
                segments = db_session.query(TranscriptSegment).filter(
                    TranscriptSegment.transcript_id == t.id
                ).order_by(TranscriptSegment.start_time).limit(30).all()

                for seg in segments:
                    if len(seg.text) > 40:  # Only substantial segments
                        results.append({
                            'video_id': str(t.video_id),
                            'video_title': video_info.get('filename', 'Unknown'),
                            'speaker': video_info.get('speaker', 'Unknown'),
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

    return unique_results


def validate_clips_against_database(clips: list) -> list:
    """Validate that clips reference real videos and reasonable timestamps."""
    validated = []

    with DatabaseSession() as db_session:
        for clip in clips:
            video_id = clip.get('video_id')
            start_time = clip.get('start_time', 0)
            end_time = clip.get('end_time', 0)

            # Check video exists
            try:
                video = db_session.query(Video).filter(Video.id == UUID(video_id)).first()
            except:
                video = None

            if video:
                # Find matching or nearby segments
                transcript = db_session.query(Transcript).filter(
                    Transcript.video_id == video.id,
                    Transcript.status == 'completed'
                ).first()

                if transcript:
                    # Get segments in the time range
                    segments = db_session.query(TranscriptSegment).filter(
                        TranscriptSegment.transcript_id == transcript.id,
                        TranscriptSegment.start_time >= start_time - 2,
                        TranscriptSegment.end_time <= end_time + 2
                    ).order_by(TranscriptSegment.start_time).all()

                    if segments:
                        # Combine segment texts for verification
                        actual_text = ' '.join(s.text for s in segments)
                        actual_start = float(segments[0].start_time)
                        actual_end = float(segments[-1].end_time)

                        validated.append({
                            'video_id': str(video.id),
                            'video_title': video.filename,
                            'speaker': video.speaker or 'Unknown',
                            'start_time': actual_start,
                            'end_time': actual_end,
                            'duration': actual_end - actual_start,
                            'text': actual_text,
                            'verified': True,
                            'original_text': clip.get('text', '')
                        })
                    else:
                        # No matching segments, mark as unverified
                        validated.append({
                            **clip,
                            'video_title': video.filename,
                            'verified': False,
                            'warning': 'Timestamps not found in transcript'
                        })
            else:
                validated.append({
                    **clip,
                    'verified': False,
                    'warning': 'Video not found in database'
                })

    return validated


def generate_script_with_ai(user_message: str, transcript_context: list, conversation_history: list):
    """Generate a script using AI with verified transcript data."""

    # Build context from transcripts
    context_text = ""
    for t in transcript_context[:300]:  # Limit context size
        context_text += f"[{t['video_title']} | {t['speaker']} | {t['start']:.1f}s-{t['end']:.1f}s]\n"
        context_text += f"Video ID: {t['video_id']}\n"
        context_text += f'"{t["text"]}"\n\n'

    system_prompt = """You are a video script assistant with access to a library of video transcripts. Your job is to help create compelling video scripts by finding and combining the best clips from the available footage.

CRITICAL RULES FOR ACCURACY:
1. ONLY use clips from the transcript data provided below
2. Use EXACT video IDs, timestamps, and text from the data
3. Never invent or approximate - if you can't find a good match, say so
4. Always include the video_id, start_time, end_time, and exact text for each clip

When creating a script:
- Pick clips that flow naturally together
- Aim for the requested duration (usually 60 seconds)
- Each clip should be a complete thought
- Build a narrative arc: hook → build → climax → close

AVAILABLE TRANSCRIPT DATA:
""" + context_text + """

When suggesting clips, format each clip as:
VIDEO: [filename]
ID: [video_id]
TIME: [start]s - [end]s ([duration]s)
TEXT: "[exact text]"

At the end, provide a JSON block with the clips:
```json
{
  "title": "Script Title",
  "total_duration": 60,
  "clips": [
    {"video_id": "...", "start_time": 10.0, "end_time": 22.0, "text": "..."}
  ]
}
```"""

    # Build messages
    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history (last 10 messages)
    for msg in conversation_history[-10:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=2000,
        )

        assistant_message = response.choices[0].message.content

        # Try to extract JSON clips from response
        clips = []
        if "```json" in assistant_message:
            try:
                json_str = assistant_message.split("```json")[1].split("```")[0]
                data = json.loads(json_str)
                clips = data.get("clips", [])
            except:
                pass

        # Validate clips against database
        if clips:
            validated_clips = validate_clips_against_database(clips)
        else:
            validated_clips = []

        return {
            "message": assistant_message,
            "clips": validated_clips,
            "has_script": len(validated_clips) > 0
        }

    except Exception as e:
        return {
            "message": f"Error generating response: {str(e)}",
            "clips": [],
            "has_script": False,
            "error": True
        }


@app.route('/chat')
def chat():
    """Chat interface for script generation."""
    return render_template('chat.html')


@app.route('/api/chat', methods=['POST'])
def api_chat():
    """Handle chat messages for script generation."""
    data = request.json
    user_message = data.get('message', '').strip()
    conversation_history = data.get('history', [])

    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    # Search for relevant transcript context
    context = search_transcripts_for_context(user_message)

    # Generate response with AI
    result = generate_script_with_ai(user_message, context, conversation_history)

    return jsonify({
        'response': result['message'],
        'clips': result.get('clips', []),
        'has_script': result.get('has_script', False),
        'context_segments': len(context)
    })


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)

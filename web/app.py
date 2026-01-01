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


def search_transcripts_for_context(query: str, limit: int = 800):
    """Search transcripts for relevant segments based on query keywords."""
    # Extract keywords from query - get meaningful words
    stop_words = {'want', 'need', 'like', 'make', 'create', 'find', 'give', 'about', 'from', 'with', 'that', 'this', 'have', 'will', 'would', 'could', 'should', 'video', 'script', 'clips', 'second', 'minute'}
    keywords = [w for w in re.findall(r'\b\w{4,}\b', query.lower()) if w not in stop_words]

    # Extract year filters from query (e.g., "2020", "2023", "from 2020")
    year_pattern = re.findall(r'\b(19\d{2}|20\d{2})\b', query)
    min_year = None
    if year_pattern:
        min_year = min(int(y) for y in year_pattern)

    # Check for "recent" or "latest" keywords
    if any(w in query.lower() for w in ['recent', 'latest', 'new', 'newest']):
        min_year = 2020

    results = []
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

        # Search for matching segments - use more keywords for better coverage
        for keyword in keywords[:8]:
            segments = db_session.query(TranscriptSegment).join(
                Transcript, TranscriptSegment.transcript_id == Transcript.id
            ).filter(
                TranscriptSegment.text.ilike(f'%{keyword}%'),
                Transcript.status == 'completed'
            ).limit(200).all()

            for seg in segments:
                transcript = db_session.query(Transcript).filter(
                    Transcript.id == seg.transcript_id
                ).first()
                if transcript and str(transcript.video_id) in filtered_video_ids:
                    video_info = video_map.get(str(transcript.video_id), {})
                    event_date = video_info.get('event_date')
                    date_str = event_date.strftime('%Y-%m-%d') if event_date else 'Unknown date'

                    results.append({
                        'video_id': str(transcript.video_id),
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

    # Build context from transcripts with full metadata
    context_text = ""
    for t in transcript_context[:300]:  # Limit context size
        context_text += f"[{t.get('event_date', 'Unknown')} | {t.get('event_name', 'Unknown')} | {t.get('speaker', 'Unknown')}]\n"
        context_text += f"Video: {t['video_title']} | {t['start']:.1f}s-{t['end']:.1f}s | ID:{t['video_id']}\n"
        context_text += f'"{t["text"]}"\n\n'

    system_prompt = f"""You are a master video editor creating compelling short-form content. Your job: craft a NARRATIVE that flows naturally, using real clips AND suggested narration to bridge gaps.

{summary}

THE PROBLEM WITH RAW CLIPS:
Raw transcript clips often don't flow together naturally. They start mid-sentence, lack context, or jump between unrelated ideas. Your job is to:
1. Select the BEST, most powerful clips
2. Add NARRATION suggestions to bridge them into a cohesive story

OUTPUT FORMAT - USE THIS EXACT STRUCTURE:

---
**[COMPELLING TITLE]**

[NARRATE: Opening hook that sets up the theme - 1-2 sentences]

[CLIP 1: "Exact quote from the transcript data"]

[NARRATE: Transition that connects clip 1 to clip 2]

[CLIP 2: "Exact quote from the transcript data"]

[NARRATE: Build toward the conclusion]

[CLIP 3: "Powerful closing quote from data"]

[NARRATE: Optional closing thought]
---

RULES:
1. [CLIP X] = EXACT text from transcript data below. Never paraphrase.
2. [NARRATE] = Your suggested voiceover/narration to bridge clips. Keep it concise (1-2 sentences max).
3. Pick clips that are COMPLETE THOUGHTS - no mid-sentence fragments
4. The narration should make the clips feel like ONE cohesive piece
5. Aim for 3-5 strong clips with narration bridges

EXAMPLE OF GOOD OUTPUT:
---
**[The Future We Build]**

[NARRATE: What does it take to shape tomorrow? The answer lies in how we lead today.]

[CLIP 1: "True leadership means having the courage to make difficult decisions."]

[NARRATE: But courage alone isn't enough. It requires vision.]

[CLIP 2: "We must ask ourselves: what do we aspire to be as a nation, as a people?"]

[NARRATE: And ultimately, it demands action.]

[CLIP 3: "The future belongs to those who believe in the beauty of their dreams."]
---

Then provide JSON with the clips only (not narration):
```json
{{
  "title": "...",
  "total_duration": 60,
  "clips": [
    {{"video_id": "exact-uuid", "video_title": "...", "start_time": 10.0, "end_time": 22.0, "text": "exact text"}}
  ]
}}
```

AVAILABLE TRANSCRIPT CLIPS:
""" + context_text

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
    try:
        data = request.json
        user_message = data.get('message', '').strip()
        conversation_history = data.get('history', [])

        if not user_message:
            return jsonify({'error': 'No message provided'}), 400

        # Search for relevant transcript context
        context = search_transcripts_for_context(user_message)

        if not context:
            return jsonify({
                'response': "I couldn't find any matching content in the video library. Try different keywords or check the Transcripts page to see what's available.",
                'clips': [],
                'has_script': False,
                'context_segments': 0
            })

        # Generate response with AI
        result = generate_script_with_ai(user_message, context, conversation_history)

        return jsonify({
            'response': result['message'],
            'clips': result.get('clips', []),
            'has_script': result.get('has_script', False),
            'context_segments': len(context)
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

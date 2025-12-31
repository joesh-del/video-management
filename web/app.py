"""Flask web application for video management dashboard."""

import os
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify, request, send_file, redirect, url_for

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
    with DatabaseSession() as session:
        videos = session.query(Video).order_by(Video.created_at.desc()).all()
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

    return render_template('videos.html', videos=video_list)


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)

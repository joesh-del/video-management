-- Migration: Add audio recordings table for Otter AI imports
-- This stores audio files with timestamped transcripts that can be searched
-- alongside video transcripts for script generation

-- Audio recordings table (similar to videos but for audio-only content)
CREATE TABLE IF NOT EXISTS audio_recordings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- File info
    filename VARCHAR(500) NOT NULL,
    original_filename VARCHAR(500) NOT NULL,
    s3_key VARCHAR(1000) NOT NULL UNIQUE,
    s3_bucket VARCHAR(255) NOT NULL DEFAULT 'mv-brain',
    file_size_bytes BIGINT,
    duration_seconds NUMERIC(10, 2),
    format VARCHAR(20),  -- mp3, wav, m4a, etc.

    -- Recording metadata
    title VARCHAR(500),
    recording_date DATE,
    speakers JSONB DEFAULT '[]',  -- List of speaker names
    keywords JSONB DEFAULT '[]',  -- Otter AI keywords
    summary TEXT,  -- Otter AI summary or AI-generated

    -- Persona association (optional - for voice matching)
    persona_id UUID REFERENCES personas(id) ON DELETE SET NULL,

    -- Source info
    source VARCHAR(50) DEFAULT 'otter_ai',  -- otter_ai, manual, zoom, etc.
    source_url TEXT,

    -- Processing status
    status VARCHAR(50) DEFAULT 'uploaded',  -- uploaded, transcribed, processed

    -- Metadata
    metadata JSONB DEFAULT '{}',
    uploaded_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Audio transcript segments (timestamped text with speaker info)
CREATE TABLE IF NOT EXISTS audio_segments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    audio_id UUID NOT NULL REFERENCES audio_recordings(id) ON DELETE CASCADE,

    -- Timing
    segment_index INTEGER NOT NULL,
    start_time NUMERIC(10, 3) NOT NULL,  -- seconds
    end_time NUMERIC(10, 3) NOT NULL,

    -- Content
    text TEXT NOT NULL,
    speaker VARCHAR(100),

    -- Search optimization
    text_search tsvector,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for efficient searching
CREATE INDEX IF NOT EXISTS idx_audio_recordings_persona ON audio_recordings(persona_id);
CREATE INDEX IF NOT EXISTS idx_audio_recordings_date ON audio_recordings(recording_date);
CREATE INDEX IF NOT EXISTS idx_audio_recordings_status ON audio_recordings(status);
CREATE INDEX IF NOT EXISTS idx_audio_segments_audio ON audio_segments(audio_id);
CREATE INDEX IF NOT EXISTS idx_audio_segments_speaker ON audio_segments(speaker);
CREATE INDEX IF NOT EXISTS idx_audio_segments_text_search ON audio_segments USING GIN(text_search);

-- Trigger to update text_search vector
CREATE OR REPLACE FUNCTION update_audio_segment_search()
RETURNS TRIGGER AS $$
BEGIN
    NEW.text_search := to_tsvector('english', COALESCE(NEW.text, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audio_segment_search_update ON audio_segments;
CREATE TRIGGER audio_segment_search_update
    BEFORE INSERT OR UPDATE ON audio_segments
    FOR EACH ROW
    EXECUTE FUNCTION update_audio_segment_search();

-- Update timestamp trigger
CREATE OR REPLACE FUNCTION update_audio_recording_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audio_recording_timestamp ON audio_recordings;
CREATE TRIGGER audio_recording_timestamp
    BEFORE UPDATE ON audio_recordings
    FOR EACH ROW
    EXECUTE FUNCTION update_audio_recording_timestamp();

-- Migration: Add personas and content tables for copy generation
-- Date: 2026-01-03

-- Personas table: Voice profiles for people (Dan Goldin, etc.)
CREATE TABLE IF NOT EXISTS personas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL UNIQUE,

    -- Manual voice configuration
    description TEXT,  -- Who is this person
    tone TEXT,  -- e.g., "authoritative but approachable", "technical and precise"
    style_notes TEXT,  -- Writing style notes
    topics JSONB DEFAULT '[]',  -- Topics they typically discuss
    vocabulary JSONB DEFAULT '[]',  -- Key phrases/words they use

    -- Auto-learned voice (populated by analyzing their content)
    learned_style JSONB DEFAULT '{}',  -- AI-generated style analysis

    -- Links to existing data
    speaker_name_in_videos VARCHAR(255),  -- Links to videos.speaker field

    -- Metadata
    avatar_url TEXT,
    is_active INTEGER DEFAULT 1,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Documents table: Articles, call transcripts, notes
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    persona_id UUID REFERENCES personas(id) ON DELETE SET NULL,

    -- Document info
    title VARCHAR(500) NOT NULL,
    doc_type VARCHAR(50) NOT NULL,  -- article, call_transcript, notes, other

    -- Content
    content_text TEXT,  -- Extracted/parsed text content
    content_summary TEXT,  -- AI-generated summary
    word_count INTEGER,

    -- Source file (if uploaded)
    source_filename VARCHAR(500),
    source_s3_key VARCHAR(1000),
    source_format VARCHAR(20),  -- pdf, docx, txt, audio

    -- For audio transcripts
    duration_seconds NUMERIC(10, 2),
    transcription_provider VARCHAR(50),  -- whisper, aws, etc.

    -- Metadata
    document_date DATE,
    source_url TEXT,
    tags JSONB DEFAULT '[]',
    extra_data JSONB DEFAULT '{}',

    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Social posts table: Historical social media posts
CREATE TABLE IF NOT EXISTS social_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    persona_id UUID REFERENCES personas(id) ON DELETE SET NULL,

    -- Post info
    platform VARCHAR(50) NOT NULL,  -- linkedin, x, facebook, other
    content TEXT NOT NULL,

    -- Platform metadata
    external_post_id VARCHAR(255),  -- ID from the platform
    post_url TEXT,
    posted_at TIMESTAMP WITH TIME ZONE,

    -- Engagement metrics (optional)
    likes INTEGER,
    comments INTEGER,
    shares INTEGER,
    impressions INTEGER,

    -- Media attachments
    media_urls JSONB DEFAULT '[]',
    screenshot_s3_key VARCHAR(1000),  -- For LinkedIn screenshots

    -- Metadata
    is_original INTEGER DEFAULT 1,  -- 1 = original post, 0 = repost/share
    hashtags JSONB DEFAULT '[]',
    mentions JSONB DEFAULT '[]',
    extra_data JSONB DEFAULT '{}',

    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_personas_name ON personas(name);
CREATE INDEX IF NOT EXISTS idx_personas_speaker ON personas(speaker_name_in_videos);

CREATE INDEX IF NOT EXISTS idx_documents_persona ON documents(persona_id);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_date ON documents(document_date);

CREATE INDEX IF NOT EXISTS idx_social_posts_persona ON social_posts(persona_id);
CREATE INDEX IF NOT EXISTS idx_social_posts_platform ON social_posts(platform);
CREATE INDEX IF NOT EXISTS idx_social_posts_posted_at ON social_posts(posted_at DESC);

-- Full-text search indexes
CREATE INDEX IF NOT EXISTS idx_documents_content_fts ON documents USING gin(to_tsvector('english', content_text));
CREATE INDEX IF NOT EXISTS idx_social_posts_content_fts ON social_posts USING gin(to_tsvector('english', content));

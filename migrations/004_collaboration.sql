-- Migration: Add collaboration features
-- Date: 2026-01-02

-- Add is_collaborative column to conversations
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS is_collaborative INTEGER DEFAULT 0;

-- Add user_id and mentions to chat_messages
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS mentions JSONB DEFAULT '[]';

-- Create chat_participants table
CREATE TABLE IF NOT EXISTS chat_participants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(20) DEFAULT 'member',
    invited_by UUID REFERENCES users(id) ON DELETE SET NULL,
    joined_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(conversation_id, user_id)
);

-- Create clip_comments table
CREATE TABLE IF NOT EXISTS clip_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id UUID REFERENCES chat_messages(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    clip_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    mentions JSONB DEFAULT '[]',
    is_regenerate_request INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create voice_avatars table
CREATE TABLE IF NOT EXISTS voice_avatars (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    speaker_name VARCHAR(255) NOT NULL UNIQUE,
    provider VARCHAR(50) DEFAULT 'elevenlabs',
    external_voice_id VARCHAR(255),
    sample_video_ids JSONB DEFAULT '[]',
    status VARCHAR(50) DEFAULT 'pending',
    settings JSONB DEFAULT '{}',
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_chat_participants_conversation ON chat_participants(conversation_id);
CREATE INDEX IF NOT EXISTS idx_chat_participants_user ON chat_participants(user_id);
CREATE INDEX IF NOT EXISTS idx_clip_comments_conversation ON clip_comments(conversation_id);
CREATE INDEX IF NOT EXISTS idx_clip_comments_message ON clip_comments(message_id);
CREATE INDEX IF NOT EXISTS idx_voice_avatars_speaker ON voice_avatars(speaker_name);

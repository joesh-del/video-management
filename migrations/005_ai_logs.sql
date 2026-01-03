-- Migration: Add AI logging for quality monitoring
-- Date: 2026-01-03

-- Create ai_logs table
CREATE TABLE IF NOT EXISTS ai_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Request metadata
    request_type VARCHAR(50) NOT NULL,  -- chat, regenerate_clip, regenerate_record, etc.
    model VARCHAR(100) NOT NULL,  -- claude-sonnet, gpt-4o, etc.

    -- Context
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,

    -- Request/Response content
    prompt TEXT,  -- The prompt sent to AI
    context_summary TEXT,  -- Summary of context provided
    response TEXT,  -- Full AI response

    -- Extracted results
    clips_generated INTEGER DEFAULT 0,  -- Number of clips in response
    response_json JSONB,  -- Parsed JSON from response

    -- Performance & Status
    success INTEGER DEFAULT 1,  -- 1 = success, 0 = failure
    error_message TEXT,  -- Error message if failed
    latency_ms FLOAT,  -- Time taken in milliseconds
    input_tokens INTEGER,  -- Token count (if available)
    output_tokens INTEGER,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_ai_logs_request_type ON ai_logs(request_type);
CREATE INDEX IF NOT EXISTS idx_ai_logs_model ON ai_logs(model);
CREATE INDEX IF NOT EXISTS idx_ai_logs_user ON ai_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_ai_logs_conversation ON ai_logs(conversation_id);
CREATE INDEX IF NOT EXISTS idx_ai_logs_success ON ai_logs(success);
CREATE INDEX IF NOT EXISTS idx_ai_logs_created_at ON ai_logs(created_at DESC);

CREATE TABLE IF NOT EXISTS groups (
    id SERIAL PRIMARY KEY,
    group_name TEXT UNIQUE NOT NULL
);

ALTER TABLE groups ADD COLUMN IF NOT EXISTS created_by TEXT;

CREATE TABLE IF NOT EXISTS reactions (
  id bigint primary key generated by default as identity,
  message_id bigint not null references messages(id) on delete cascade,
  username text not null,
  reaction text not null,
  created_at timestamptz default now()
);
-- Read-only role for the dashboard / future API. Safe to run more than once.
--
-- ap_ui_readonly has SELECT only, on the trex and agent schemas. It cannot log in by itself and
-- no password lives in this file. All data in this project is fake.
-- It can read agent.audit_log and agent.processed_emails (sender, subject), by design.
--
-- To create a login user (Supabase: Dashboard > SQL editor, run as the postgres user):
--     CREATE ROLE ap_ui LOGIN PASSWORD '<generate-a-long-random-password>' IN ROLE ap_ui_readonly;
-- Then put its connection string (same host as DATABASE_URL, user ap_ui, or ap_ui.<project-ref>
-- on the Supabase pooler) in .env as UI_DATABASE_URL. For local dev the UI falls back to
-- DATABASE_URL.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ap_ui_readonly') THEN
        CREATE ROLE ap_ui_readonly NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA trex, agent TO ap_ui_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA trex, agent TO ap_ui_readonly;

-- Tables created later by the role that runs migrations are covered too.
ALTER DEFAULT PRIVILEGES IN SCHEMA trex GRANT SELECT ON TABLES TO ap_ui_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA agent GRANT SELECT ON TABLES TO ap_ui_readonly;

-- The client's own role, cdse_loader. It owns the iris_staging schema and may use nothing else:
-- it cannot create schemas or tables anywhere outside iris_staging, so the database itself
-- enforces "write only to iris_staging", not just this program's code.
-- PostGIS functions and types live in public; every role may use them (USAGE), none may create there.
-- Run as the database owner, once per database: psql -v password=<the client's password> -f client_role.sql
-- Safe to run again, also on a database created before this file existed.

SELECT format('CREATE ROLE cdse_loader LOGIN PASSWORD %L', :'password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cdse_loader') \gexec
SELECT format('ALTER ROLE cdse_loader LOGIN PASSWORD %L', :'password') \gexec

CREATE SCHEMA IF NOT EXISTS iris_staging AUTHORIZATION cdse_loader;
ALTER SCHEMA iris_staging OWNER TO cdse_loader;
DO $$
DECLARE
    item record;
BEGIN
    -- Tables made before the role existed are handed over, so migrations can still change them.
    -- Their id sequences belong to them and move along.
    FOR item IN
        SELECT format('ALTER TABLE iris_staging.%I OWNER TO cdse_loader', c.relname) AS statement
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'iris_staging' AND c.relkind IN ('r', 'p')
    LOOP
        EXECUTE item.statement;
    END LOOP;
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;  -- already so since PostgreSQL 15; stated for older servers
GRANT USAGE ON SCHEMA public TO cdse_loader;
GRANT CONNECT ON DATABASE :"DBNAME" TO cdse_loader;

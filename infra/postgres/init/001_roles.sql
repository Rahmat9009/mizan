-- =============================================================================
-- 001_roles.sql -- administrative role and schema. Idempotent.
--
-- Runs once at first init as the bootstrap superuser (POSTGRES_USER) via
-- /docker-entrypoint-initdb.d, connected to POSTGRES_DB. In CI it is applied with
--   psql -X -v ON_ERROR_STOP=1 -f infra/postgres/init/001_roles.sql
-- against the service database. :"DBNAME" is psql's built-in variable for the
-- current database, so this file is not tied to one database name.
--
-- Roles (Hard Rules B3 per-tenant isolation, A2 append-only):
--
--   mizan_admin       NOLOGIN. Owns the database, the mizan_admin schema, the
--                     provisioning function and every tenant schema and table.
--                     CREATEROLE so it can mint per-tenant application roles.
--                     NOT a superuser. Operators are GRANTed membership.
--
--   tenant_<id>_app   NOLOGIN, created per tenant by mizan_admin.create_tenant().
--                     USAGE on its own schema only; SELECT + INSERT on its tables;
--                     UPDATE / DELETE / TRUNCATE explicitly revoked. It owns nothing,
--                     so it cannot drop or disable the append-only triggers.
--
-- The application NEVER connects as the bootstrap superuser. The operator creates
-- a LOGIN role and grants it tenant_<id>_app (see infra/postgres/README.md).
-- No password appears anywhere in this repository.
-- =============================================================================

\set ON_ERROR_STOP on

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'mizan_admin') THEN
    CREATE ROLE mizan_admin
      NOLOGIN NOSUPERUSER CREATEROLE NOCREATEDB NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
END
$do$;

-- mizan_admin owns the database so that it can create tenant schemas and grant
-- CONNECT to tenant roles without being a superuser.
ALTER DATABASE :"DBNAME" OWNER TO mizan_admin;

-- Nobody connects or creates objects by default. Tenant app roles receive CONNECT
-- explicitly from create_tenant(); operators receive it through mizan_admin.
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO mizan_admin;

-- The public schema is not a place for anything. PostgreSQL 15+ already removed
-- CREATE from PUBLIC; USAGE is removed here too so tenant roles cannot see it.
REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA IF NOT EXISTS mizan_admin AUTHORIZATION mizan_admin;
REVOKE ALL ON SCHEMA mizan_admin FROM PUBLIC;

-- Registry of provisioned tenants. Admin-only: tenant app roles have no USAGE on
-- this schema and therefore cannot even enumerate other tenants.
CREATE TABLE IF NOT EXISTS mizan_admin.tenants (
  tenant_id   text PRIMARY KEY CHECK (tenant_id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  schema_name text NOT NULL UNIQUE,
  app_role    text NOT NULL UNIQUE,
  created_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE mizan_admin.tenants OWNER TO mizan_admin;
REVOKE ALL ON mizan_admin.tenants FROM PUBLIC;

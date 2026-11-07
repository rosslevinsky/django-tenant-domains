-- Local and CI test roles only. The passwords are not secrets.
-- dtd_app is the ordinary role row-level security binds.
CREATE ROLE dtd_app LOGIN PASSWORD 'dtd_app';
-- dtd_migrator runs migrations and admin tasks, and bypasses row-level security.
CREATE ROLE dtd_migrator LOGIN PASSWORD 'dtd_migrator' CREATEDB BYPASSRLS;
CREATE DATABASE dtd OWNER dtd_migrator;

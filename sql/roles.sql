-- Least-privilege database roles for the agent.
-- agent_ro:     SELECT only. Every model-written query runs as this role.
-- agent_writer: may INSERT substitution proposals and nothing else.
-- Passwords are for local development only; change them anywhere else.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
        CREATE ROLE agent_ro LOGIN PASSWORD 'agent_ro';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_writer') THEN
        CREATE ROLE agent_writer LOGIN PASSWORD 'agent_writer';
    END IF;
END
$$;

ALTER ROLE agent_ro SET default_transaction_read_only = on;
ALTER ROLE agent_ro SET statement_timeout = '5s';
ALTER ROLE agent_writer SET statement_timeout = '5s';

GRANT USAGE ON SCHEMA public TO agent_ro, agent_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;

GRANT SELECT ON products, inventory, order_items, orders, customers, substitution_proposals TO agent_writer;
GRANT INSERT ON substitution_proposals TO agent_writer;
GRANT USAGE ON SEQUENCE substitution_proposals_proposal_id_seq TO agent_writer;

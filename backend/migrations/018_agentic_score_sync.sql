-- ============================================================================
-- CampusLens - migration 018 - agentic score synchronization
--
-- Unifies the complaint's priority score with the deterministic agentic
-- priority score. When the agent pipeline completes (either during submission
-- or on rerun), the agentic score and its breakdown are persisted directly
-- to public.complaints.priority_score and public.complaints.score_breakdown.
-- ============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION public.store_agentic_score(
    p_complaint_id uuid,
    p_score numeric,
    p_breakdown jsonb
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $func$
BEGIN
    UPDATE public.complaints
       SET priority_score = p_score,
           score_breakdown = p_breakdown,
           score_computed_at = now()
     WHERE id = p_complaint_id;
END;
$func$;

REVOKE ALL ON FUNCTION public.store_agentic_score(uuid, numeric, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.store_agentic_score(uuid, numeric, jsonb) TO authenticated;

COMMIT;

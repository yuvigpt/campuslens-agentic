-- Additive Agentic observability schema. Apply after 016.
-- Agent output is advisory; no trigger here changes complaint status or score.
BEGIN;

CREATE TABLE IF NOT EXISTS public.agent_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  complaint_id uuid NOT NULL REFERENCES public.complaints(id) ON DELETE CASCADE,
  workflow_version text NOT NULL DEFAULT 'agentic-v1',
  status text NOT NULL CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED','TIMEOUT','SKIPPED')),
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  duration_ms integer,
  confidence numeric CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  human_review_required boolean NOT NULL DEFAULT false,
  result jsonb,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.agent_steps (
  id bigserial PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES public.agent_runs(id) ON DELETE CASCADE,
  agent_name text NOT NULL,
  status text NOT NULL CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED','TIMEOUT','SKIPPED')),
  started_at timestamptz,
  completed_at timestamptz,
  duration_ms integer,
  confidence numeric CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  uncertainty jsonb NOT NULL DEFAULT '[]'::jsonb,
  output jsonb,
  error_code text
);

CREATE TABLE IF NOT EXISTS public.agent_reviews (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  complaint_id uuid NOT NULL REFERENCES public.complaints(id) ON DELETE CASCADE,
  run_id uuid REFERENCES public.agent_runs(id) ON DELETE SET NULL,
  status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','IN_REVIEW','APPROVED','MODIFIED','REJECTED')),
  reviewer_id uuid REFERENCES public.profiles(id),
  decision jsonb,
  reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  reviewed_at timestamptz
);

CREATE INDEX IF NOT EXISTS agent_runs_complaint_idx ON public.agent_runs(complaint_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_steps_run_idx ON public.agent_steps(run_id, id);
CREATE INDEX IF NOT EXISTS agent_reviews_queue_idx ON public.agent_reviews(status, created_at DESC);

ALTER TABLE public.agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_reviews ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_runs_admin_read ON public.agent_runs;
CREATE POLICY agent_runs_admin_read ON public.agent_runs FOR SELECT TO authenticated USING (public.is_admin());
DROP POLICY IF EXISTS agent_runs_owner_insert ON public.agent_runs;
CREATE POLICY agent_runs_owner_insert ON public.agent_runs FOR INSERT TO authenticated
  WITH CHECK (public.is_admin() OR EXISTS (
    SELECT 1 FROM public.complaints c
    WHERE c.id = complaint_id AND c.student_id = auth.uid()
  ));
DROP POLICY IF EXISTS agent_steps_admin_read ON public.agent_steps;
CREATE POLICY agent_steps_admin_read ON public.agent_steps FOR SELECT TO authenticated USING (public.is_admin());
DROP POLICY IF EXISTS agent_steps_owner_insert ON public.agent_steps;
CREATE POLICY agent_steps_owner_insert ON public.agent_steps FOR INSERT TO authenticated
  WITH CHECK (public.is_admin() OR EXISTS (
    SELECT 1 FROM public.agent_runs r
    JOIN public.complaints c ON c.id = r.complaint_id
    WHERE r.id = run_id AND c.student_id = auth.uid()
  ));
DROP POLICY IF EXISTS agent_reviews_admin_all ON public.agent_reviews;
CREATE POLICY agent_reviews_admin_all ON public.agent_reviews FOR ALL TO authenticated USING (public.is_admin()) WITH CHECK (public.is_admin());

COMMIT;

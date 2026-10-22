-- Self-host override of 023: Postgres has no CREATE POLICY IF NOT EXISTS.
ALTER TABLE crawl_jobs ADD COLUMN IF NOT EXISTS pdf_storage_url TEXT;

INSERT INTO storage.buckets (id, name, public)
VALUES ('regulation-pdfs', 'regulation-pdfs', true)
ON CONFLICT (id) DO NOTHING;

DROP POLICY IF EXISTS "Public read regulation PDFs" ON storage.objects;
CREATE POLICY "Public read regulation PDFs"
  ON storage.objects FOR SELECT
  USING (bucket_id = 'regulation-pdfs');

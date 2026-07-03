from __future__ import annotations

import os

# Settings() требует SUPABASE_URL/SUPABASE_ANON_KEY при импорте app.core.config —
# задаём фиктивные значения ДО того, как что-либо в app/ будет импортировано.
os.environ.setdefault('SUPABASE_URL', 'https://fake-project.supabase.co')
os.environ.setdefault('SUPABASE_ANON_KEY', 'fake-anon-key')

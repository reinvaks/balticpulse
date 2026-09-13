from energy_sources import SourceStatus
import inspect
sig = inspect.signature(SourceStatus)
params = set(sig.parameters)
assert params == {'ok', 'error', 'note', 'status_code', 'url', 'source', 'fetched_at'} or params.issuperset({'ok', 'error', 'note', 'status_code', 'url', 'source', 'fetched_at'})
print('SourceStatus constructor contract: OK', sig)

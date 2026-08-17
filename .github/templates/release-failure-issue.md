## 🚨 nlm-ckn-etl production release failed

The `production` deployment for ETL run **`{version}`** reported state **`{state}`**.

`nlm-ckn-ui/ETL_VERSION` was **not** bumped - the UI still points at the previously released dataset.

### Status reported by `release.py`
{status_fence}

### The release this came from
{release_block}

### Where to look
{where_block}

{raw_description_block}### Notes
- Several failure statuses can be posted for a single run; each one appears as a comment here rather than as a new issue.
- Close this issue once the release has been re-run successfully - a successful release does not close it automatically.
- Releases killed at the infrastructure level (Batch timeout, OOM, spot reclaim, image pull failure) post **no** status at all and so produce **no** issue: the absence of an issue is not proof of success.

_Filed automatically by `.github/workflows/bump-ui-etl-version.yml`._

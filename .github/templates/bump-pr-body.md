## 📦 New ETL dataset available - pin `ETL_VERSION` to `{version}`

{lede}

### What this changes
`{sibling_path}` (repo root) → `{version}`

`scripts/app/deploy-dataset.sh` rebuilds the dataset key from that file, so this one line is what decides which graph the deployed app serves. It pins exactly one object in the ETL release bucket:

```text
{dump_key}
```

### Where this dataset came from
{provenance_block}

### What merging this does
Merging to `main` changes `{sibling_path}`, which is the path filter on this repo's `.github/workflows/deploy-dataset.yml`. That workflow runs `scripts/app/deploy-dataset.sh dev`, which restores the dump above into a *green* ArangoDB container on the dev EC2 instance via SSM while *blue* keeps serving, then swaps green in and only then updates the `dataset-version` SSM parameter.

Budget real time for it: the restore itself is typically 5-15 minutes, and the job allows up to 110 minutes (90-minute SSM timeout) for a large dump. Nothing else is deployed - the application image is untouched.

### If this is wrong
Nothing moves until this PR merges.
- **Not ready to promote this dataset?** Leave the PR open, or close it. The running dev dataset is unaffected either way.
- **Dataset is bad?** Close the PR and re-run the ETL release. A later successful release force-updates this same branch, so this PR (or its replacement) always reflects the most recent successful run.
- **Already merged and regretting it?** A failed restore leaves the `dataset-version` parameter on the previously live version and blue keeps serving; to go back deliberately, revert this file and merge.

_Opened automatically by `.github/workflows/bump-ui-etl-version.yml` in `nlm-ckn-etl`. Not auto-merged by design._

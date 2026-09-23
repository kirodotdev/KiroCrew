# Migrating from legacy `meshclaw-deploy-*` stacks

Accounts that used the pre-rename deploy tooling carry `meshclaw-deploy-base`
and `meshclaw-deploy-reaper` stacks. Current code only recognizes the
`kirocrew-` prefix, so a first deploy with the new tooling creates a second,
parallel base stack. The old and new CloudFront and S3 resources coexist until
the old set is removed.

Stack-name prefixes are deliberately NOT configurable: the reaper's identity
gates (OAC name fullmatch, `kirocrew:site` tag checks, bucket-prefix
cross-validation) are written against the fixed prefix. Making the prefix an
environment override would reopen the spoofing surface those gates close.

## 1. Identify legacy resources

```bash
aws cloudformation describe-stacks --stack-name meshclaw-deploy-base \
  --profile <P> --region <R> --query 'Stacks[0].StackStatus'
aws cloudformation describe-stacks --stack-name meshclaw-deploy-reaper \
  --profile <P> --region <R> --query 'Stacks[0].StackStatus'
```

A `ValidationError` ("does not exist") for both means nothing to migrate.

## 2. Move any live sites first

Legacy deployments keep serving from the old distribution. Redeploy each one
with the installed `artifact-deploy` operator scripts: use `deploy.sh` for a
static site or `deploy-app.sh` for an app with a backend. These are the paths
that place content behind the `kirocrew-deploy-base` distribution; the dashboard
artifact-publish path uses separate per-site infrastructure. Finite-TTL deploys
require the new `kirocrew-deploy-reaper` stack; for a persistent static migration,
pass `--ttl 0`. Verify the new URL before tearing the old set down.

## 3. Tear down the legacy set (operator step)

Empty the old bucket first — CloudFormation cannot delete a non-empty bucket:

```bash
OLD_BUCKET=$(aws cloudformation describe-stacks --stack-name meshclaw-deploy-base \
  --profile <P> --region <R> \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text)
aws s3 rm "s3://${OLD_BUCKET}" --recursive --profile <P> --region <R>

aws cloudformation delete-stack --stack-name meshclaw-deploy-reaper \
  --profile <P> --region <R>
aws cloudformation wait stack-delete-complete --stack-name meshclaw-deploy-reaper \
  --profile <P> --region <R>
aws cloudformation delete-stack --stack-name meshclaw-deploy-base \
  --profile <P> --region <R>
```

Base-stack deletion takes several minutes (CloudFront disable + removal).
Confirm with:

```bash
aws cloudformation describe-stacks --stack-name meshclaw-deploy-base \
  --profile <P> --region <R> 2>&1 | grep -q 'does not exist' && echo CLEAN
```

## 4. Leftovers outside the stacks

Per-site buckets created by very old versions are named `meshclaw-web-*`.
List and remove any that remain after their sites were migrated:

```bash
aws s3api list-buckets --profile <P> \
  --query "Buckets[?starts_with(Name, 'meshclaw-web-')].Name" --output text
```

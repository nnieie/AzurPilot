#!/usr/bin/env bash
# 在 dev -> master Squash 发布后，只补充提交祖先关系，不改动 dev 文件树。
set -euo pipefail

: "${GITHUB_REPOSITORY:?missing GITHUB_REPOSITORY}"
: "${GITHUB_EVENT_NAME:?missing GITHUB_EVENT_NAME}"
: "${GH_TOKEN:?missing GH_TOKEN}"

note() {
  echo "$*"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    echo "$*" >> "$GITHUB_STEP_SUMMARY"
  fi
}

case "$GITHUB_EVENT_NAME" in
  pull_request)
    pr_number="${PR_EVENT_NUMBER:-}"
    ;;
  workflow_dispatch)
    pr_number="${MANUAL_PR_NUMBER:-}"
    ;;
  push)
    # 首次启用工作流也能通过 master 的 push 事件处理当次 Squash。
    pr_number="$(gh api "repos/$GITHUB_REPOSITORY/commits/$GITHUB_SHA/pulls" \
      | jq -r --arg repo "$GITHUB_REPOSITORY" --arg sha "$GITHUB_SHA" \
        '[.[] | select(.merged_at != null and .merge_commit_sha == $sha and .base.ref == "master" and .head.ref == "dev" and .head.repo.full_name == $repo)][0].number // ""')"
    if [[ -z "$pr_number" ]]; then
      note '此次 master push 不是已合并的 dev -> master PR，跳过。'
      exit 0
    fi
    ;;
  *)
    echo "Unsupported event: $GITHUB_EVENT_NAME" >&2
    exit 1
    ;;
esac

if [[ ! "$pr_number" =~ ^[1-9][0-9]*$ ]]; then
  echo "Invalid PR number: $pr_number" >&2
  exit 1
fi

pr="$(gh api "repos/$GITHUB_REPOSITORY/pulls/$pr_number")"
read_field() { jq -r "$1 // empty" <<< "$pr"; }

if [[ "$(read_field '.merged')" != true \
   || "$(read_field '.base.ref')" != master \
   || "$(read_field '.head.ref')" != dev \
   || "$(read_field '.head.repo.full_name')" != "$GITHUB_REPOSITORY" ]]; then
  echo "PR #$pr_number is not a merged dev -> master PR from this repository" >&2
  exit 1
fi

pr_head="$(read_field '.head.sha')"
merge_sha="$(read_field '.merge_commit_sha')"
if [[ ! "$pr_head" =~ ^[a-f0-9]{40}$ || ! "$merge_sha" =~ ^[a-f0-9]{40}$ ]]; then
  echo "Missing valid PR head / merge commit SHA" >&2
  exit 1
fi

if [[ "$GITHUB_EVENT_NAME" == push && "$merge_sha" != "$GITHUB_SHA" ]]; then
  note 'master 的新提交不是当前 PR 对应的 Squash，跳过。'
  exit 0
fi

# 始终使用远端最新 dev，不能以事件中的旧 head 覆盖后续开发提交。
git fetch --no-tags origin \
  '+refs/heads/dev:refs/remotes/origin/dev' \
  '+refs/heads/master:refs/remotes/origin/master'
git checkout -B dev refs/remotes/origin/dev

if git merge-base --is-ancestor "$merge_sha" HEAD; then
  note "PR #$pr_number 的 Squash 提交已经是 dev 祖先，无需同步。"
  exit 0
fi
if ! git cat-file -e "${pr_head}^{commit}" 2>/dev/null \
   || ! git cat-file -e "${merge_sha}^{commit}" 2>/dev/null; then
  echo 'PR source or squash commit is unavailable; refusing to change dev' >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$pr_head" HEAD; then
  echo 'dev no longer descends from the merged PR head; refusing to change history' >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$merge_sha" refs/remotes/origin/master; then
  echo 'Squash commit is not in current master; refusing to change history' >&2
  exit 1
fi

# 普通 merge 会有两个父提交，不能作为 Squash 重新接入。
if [[ "$(git rev-list --parents -n 1 "$merge_sha" | wc -w)" -ne 2 ]]; then
  note '合并结果不是单父提交（非 Squash），无需自动修复。'
  exit 0
fi

source_tree="$(git rev-parse "${pr_head}^{tree}")"
merged_tree="$(git rev-parse "${merge_sha}^{tree}")"
if [[ "$source_tree" != "$merged_tree" ]]; then
  echo "Squash tree ($merged_tree) != merged PR head tree ($source_tree)" >&2
  echo 'File contents differ; manual review required, no changes made to dev' >&2
  exit 1
fi

# 经过上述验证后，-s ours 是安全的：记录 master 的 Squash 为第二父提交，
# 保留 dev 目前全部文件和在 PR 合并后继续开发的新提交。
original_tree="$(git rev-parse 'HEAD^{tree}')"
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git merge --no-ff --no-edit -s ours \
  -m "chore(merge): record master squash ancestry after PR #$pr_number" \
  "$merge_sha"
if [[ "$(git rev-parse 'HEAD^{tree}')" != "$original_tree" ]]; then
  echo 'Unexpected working tree changes; refusing push' >&2
  exit 1
fi

# 非强制推送；dev 被其他人推进时推送将失败，不会覆盖别人的提交。
git push origin HEAD:refs/heads/dev
note "PR #$pr_number：已记录 Squash $merge_sha 的祖先关系；dev 文件树未修改。"

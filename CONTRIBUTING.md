# Branching & Release Flow

このリポジトリは GitHub Flow（单人運用に調整）で運営する。開発版と安定版を分離し、
安定版 `main` は CI を通ったRELEASEのみが反映される。

## 分支

| 分支 | 役割 | 保護 |
|------|------|------|
| `main` | 安定版。release のみ。`v*` tag を打つ | PR 必須・CI `verify-replay` green 必須・force push 禁止 |
| `develop` | 開発版（日常ここ）。機能が緑で積まれる | CI 必須・force push 禁止・直接 push 可 |
| `feat/*` `fix/*` | タスク単位の作業分支 | — |

## 日常の流れ

1. `develop` から作業分支を切る
   ```bash
   git switch develop && git pull
   git switch -c feat/short-name
   ```
2. ローカルで `make ci` を green にする（venv を使えば `make ci` のみ）
3. push して **PR を develop へ**。CI `verify-replay` green で merge
4. 一定のまとまりが develop で安定したら **release PR: develop → main**
   - version を上げ（`pyproject.toml` / CHANGELOG）、 maintainer が merge
   - `git tag -a vX.Y.Z` を打ち push、`v*` tag = 安定版スナップショット

## 判定の所在

分支は「どのコードが安定か」の目印にすぎない。契約・証跡・レビューの判定は
controller の ledger にある。分支を増やしても審批範囲・受入基準は変わらない。

## 命名

- 分支: `feat/…` `fix/…` `chore/…`（conventional commits に併せ）
- tag: `vX.Y.Z`（SemVer）。詳細はプロジェクトの versioning 規約に従う

# Executor seam — 実行体の差し替え可能な継ぎ目

Status: A' 実装済み（pi / cmd backend、stub backend で E2E test 合格）。
計画は [roadmap-2026-11.md](roadmap-2026-11.md)。

## 目的

コントローラ（本ハーネス）と実行体（モデルセッション）の境界を薄い JSON シームに固定し、
pi・Codex・将来の deepseek-harness (`dsh` の `acp` / `headless`) を同一契約で差し替えられるようにする。
コントローラ側は実行体依存を持たない。実行体側は状態 DB を触らない。

## シーム契約（v1）

実行体アダプタは信頼されたローカル主体として、公式 CLI のみを介してコントローラと会話する。

```text
adapter -> controller:  claim(task, role, actor)
controller -> adapter:  claim packet（lease token / workspace / criteria / forbidden）
adapter   -> backend:   stdin なし、cwd=lease.workspace、prompt 引数で渡す
backend   -> adapter:   exit code と stdout（reviewer は最終行に strict JSON verdict）
adapter   -> controller: report / review（lease token と packet の revision をそのままバインド）
```

- **backend** は任意の argv。既定は `pi --print --no-session`。
  - worker: 既定ツール（read/bash/edit/write）。workspace 内のみ編集可（scope 強制はコントローラ）。
  - reviewer: `--tools read,bash`。verdict JSON は
    `{"verdict":"pass|changes|needs_decision","covered_criteria":[...],"findings":[...]}`。
- verdict が不正・解析不能な場合は**提出しない**。lease 期限切れから recover で回収する。
- worker backend の失敗・timeout は `needs_decision` として人に返す。合否の判定は
  コントローラのクリーンコピー検証と独立レビューのみが行う。アダプタの自己申告はtrustしない。

## _pi を worker にする（実運用）_

```sh
python3 -m scripts.executor_pi --runtime <runtime> --task <id> --loop \
  --worker-actor pi-worker --reviewer-actor pi-reviewer [--model <pattern>]
```

`--loop` は ready → worker → verify → review_ready → reviewer → completed まで
直列に進める。単発は `--mode worker|reviewer`。tier→モデルの対応は実行体側の設定が持ち、
アダプタは `--model` で明示上書きできるだけ（設計の「routing は executor 側」を維持）。

## 境界と誠実な限界

- アダプタはコントローラ CLI subprocess を呼ぶ。同じ信頼されたローカルユーザーの
  権限で動く。敵対的 actor の隔離は OS/tool sandbox の責任（design.md の権限節と同じ）。
- worker セッションの bash はネットワーク到達がありうる。pilot では禁止行動はプロンプトと
  検証で検出し、強制は sandbox に依存する。敵対環境では実行しない。
- dsh を接続する場合は ACP ヘッドレスを別ユーザー/コンテナで動かし、
  同一シーム（claim packet in / report out）に評価アームとして登録する。

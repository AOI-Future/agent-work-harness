# 検証記録

2026-09-30 JST。対象はローカルコード開発pilot。

## Standards

初回の独立Standards reviewで再現可能な2件を検出。

- directory→file置換でreview用コピーを作れない。空ディレクトリの除去と双方向の置換testで修正。
- 長い検証中のSQLite transactionで他タスクのclaimを止める。期限付き検証予約、transaction外の
  コマンド実行、token照合による確定へ変更。並行claimと二重検証拒否のtestを追加。

## Spec

初回の独立Spec reviewで再現可能な2件を検出。

- workerの偽のテストbytecodeが受入結果を偽装できる。クリーンな検証コピー、独立cache prefix、
  bytecode書き込み禁止を導入。mtime/sizeが正しい偽bytecodeの再現testを追加。
- 引き継ぎ前の作者がreviewerになれる。全実装actorの履歴を保持して除外するtestを追加。

Standards 2件、Spec 2件を修正。最終候補の独立再レビュー結果と実行結果を下に追記する。

## 実タスク

`schema-export-pilot`: CLIからTaskSpec/WorkerReport/ReviewReport schemaを取得する追加開発。
固定した受入testが最初に失敗することを確認し、専用workerコピーで実装。
schemaの挙動、全既存behavior、lintに合格し、別セッションreviewで全criterionを確認してcompleted。
実装1 attempt、review 1 attempt。費用は未取得。開始依頼後の追加の人の介入は0件。
初回記録の完了時間は約294秒。単一タスクの観測であり、一般的な性能指標にはしない。
最新状態と対象revisionは[operations.md](operations.md)のinspect/evidenceで読み戻せる。

## 未確認

- Luna/Sol/Astra別の成功率・費用・手戻り比較と過去タスク評価。
- 既存Hermes/Supabase queueや本番への組み込み。
- 敵対的な同一OSユーザーからcontrollerを隔離する強制的な権限分離。
- 元リポジトリへの自動反映、PR/push/merge、公開。

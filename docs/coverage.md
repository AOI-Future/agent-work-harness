# 要求との対応

| 要求 | 実装/検証 |
| --- | --- |
| 短い入口・必要なdocs | AGENTS.md、docs、リンク/行数lint |
| ワンコマンド検証 | make verify: lint、構造検査、型チェック、受入テスト |
| 好み・依存方向の機械化 | Ruff、構造lint、protected/allowed paths |
| 小さなタスクと完了条件 | 不変TaskSpec、scope、criterion、dependencies |
| 生成と評価の分離 | 別actor、別workspace、全criteria coverage |
| モデルrouting | specified/design、smallからstrong、回数上限 |
| 引き継ぎと永続状態 | JSON packet/report、SQLite transaction/journal |
| 復旧・重複防止 | immutable ID、exclusive lease、token fencing、別コピーで再開 |
| revisionに束縛する証拠 | HEAD+tree digest、検証前後・review前後・完了後の照合 |
| ハーネス自体の受入テスト | tests/test_runtime.py、CLI検証、実行例 |
| 独立レビューの実効性 | 元依頼/criterion/check結果、actionable findings、同一actor拒否 |
| ツールの使いやすさ | 固定JSON形式、schema export、入力厳格化、safe error code/next action |
| 並列化の範囲分離 | claimごとの独立コピー、SQLiteの排他transaction |
| 介入と通知 | human_count/seconds、費用null、状態変化notifications+cursor |
| 過去タスク評価 | 評価manifestのtemplate。実タスク比較は今後の運用データで測定 |
| 公開・本番・課金・認証 | 実行機能を設けず既存承認境界を維持 |

実タスク`schema-export-pilot`で、root実装→自動検証→別セッションreview→completedを実行した。
実モデルを使った実装/reviewの一例であり、モデル比較やLuna性能の測定とは区別する。

## 現時点の限界

taskごとのGitコピーはworktreeよりディスクを使うが、元repoの管理情報に書き込まず分離できる。
同一OSユーザーの敵対的worker、actorなりすまし、controller DB直接改変はこのpilotでは隔離できない。
OS/tool sandboxとcontroller権限の分離を伴わない環境へ無人で展開しない。
複数モデルの実測比較、既存の業務queueへの接続、外部操作、production rolloutは未実施。

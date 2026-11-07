# Roadmap 2026-11 — 実戦投入と証拠ベース選定

作成 2026-11-07。2026-09-30 の pilot 完了（`schema-export-pilot`）以降の計画。
深層調査は deepseek-harness (dsh) 評価を含む。設計の根拠は [design.md](design.md)、
運用は [operations.md](operations.md)。

## 決定事項（2026-11-07 確定）

1. **dsh との入れ換えは行わない。** dsh は汎用実行体（executor/製品）、
   本ハーネスは制御平面（契約・証拠・独立レビュー）でカテゴリーが違う。
   dsh 側に受入条件のイミュータビリティ・revision バインド証拠・
   レビュアー独立性は存在しない。
2. **dsh は差し替え可能な実行体として seam 越しに受け入れる。**
   seam 仕様は [executor-seam.md](executor-seam.md)。
3. dsh は公式 SAFETY.md で「security audit 未実施・破壊的変更あり」を明言。
   安定化・監査情報の確認まで深結合は禁止。定点観測のみ。
4. 制御平面（本ハーネス）は保持し続ける。再発明しない。

## A'. 実行体アダプタ（この pi を worker にする）

実行体を薄い JSON シームで抽象化し、pi を最初の実装として接続する。
dsh の `acp` / `headless` profile を将来そのまま第2の実行体として
差し込める形を維持する。

- [x] seam 仕様確定: [executor-seam.md](executor-seam.md)
- [x] `scripts/executor_pi.py`: worker / reviewer / loop の3モード
- [x] モデル不要の stub backend で E2E test（`tests/test_executor.py`）
- [ ] 実 pi セッションでの1件目 E2E（有料。人間のGO待ち）
- [ ] 運用手順の追記: [operations.md](operations.md)

## B'. 評価データ収集

観測が1件（schema-export-pilot）しか無い現状を脱する。

- [ ] 実バックログ → TaskSpec インテーク手順の確定（難易度・tier を登録時固定）
- [ ] `scripts/eval_report.py`: tier 別 attempt 数・修正回数・所要時間・費用の
      比較レポート生成（未報告費用をゼロにしない既存方針を維持）
- [ ] 固定リプレイスイート拡充: `examples/schema-export-replay.json` に続き5〜10件
- [ ] 評価アームに dsh + DeepSeek を登録（yaml 定義は先行、実測は Node 環境整備後）
- [ ] ハーネス変更時のリプレイを CI 化

## C'. 統合の自動化（human gate 維持）

completed から元リポジトリ反映までの人手パッチ手渡しを、ゲート付き半自動化にする。

- [ ] `integrate` 段階追加: `completed -> integrate_ready -> integrated`
- [ ] クリーン clone への patch 適用 + 統合後状態での受入コマンド再実行 + receipt
- [ ] conflict・境界超過は `waiting_human` に落とす test
- [ ] push / merge / 公開を引き続き構造的に持たない境界として明文化
- [ ] イベントテーブルの追記専用化・再生可能化（dsh の durable event ログPatterns 移植）

## dsh 定点観測チェックリスト（深結合解禁の条件）

- developer preview 表記の外れた正式リリース
- security audit の実施情報
- ACP profile の安定性（session format migrations の落ち着き）
- 我々の seam を通した B' 実測で、同一契約での勝敗データが揃った時点

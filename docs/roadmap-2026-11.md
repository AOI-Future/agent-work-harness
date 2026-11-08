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
- [x] 実 pi セッションでの E2E: r2（verified・1試行）→ r3b（無傷チェーンで integrated
      `77a40d9`）。地は local oMLX（Qwen3.8-Flash-Next）で非課金。cloud 課金版との
      比較は評価アーム整備時に実施）
- [x] 運用手順の追記: [operations.md](operations.md)

## B'. 評価データ収集

観測が1件（schema-export-pilot）しか無い現状を脱する。

- [x] 実バックログ → TaskSpec インテーク手順の確定（難易度・tier を登録時固定）
- [x] **submit 前の check preflight（必須化）**: submit 時に check argv を
      baseline workspace で dry-run し、`rc=0` と「テスト収集数が 0 でないこと」を
      確認する。未実装のまま進めた結果、`webhook-json-output` 第1回実行で
      `unittest discover` が pytest 風テストを 0 件収集（rc=5）し、ワーカーが3試行
      無駄になった。check 自体が未検証であることが失敗の主因。
      `webhook-json-output-r2` で check 修正後、1試行で verified・9.3 分。
      **r3/r3b で手運用として必須実施済み**（submit 直前に disposable clone で
      `rc=0・collected=10` を確認）。**CLI `submit` 内の自動化を `28fea86` で実装**
      （`--preflight enforce|record|skip`、既定 enforce。結果は check tail 込みで
      submitted イベントに逐語記録）。
- [x] `scripts/eval_report.py`: tier 別 attempt 数・修正回数・所要時間・human_seconds・
      preflight 成否の比較レポート（read-only 台帳アクセス。`28fea86`）
      （未報告費用をゼロにしない既存方針を維持）
- [x] 固定リプレイスイート拡充: `examples/replay/` に固定ケース6件＋実行器
      `scripts/replay_suite.py`（happy 全連鎖 / self-review 拒否 / stale-token 拒否 /
      scope 逸脱待機 / preflight enforce 拒否 / record 正直受理）。`make replay` で6/6
- [x] 評価アームに dsh + DeepSeek を登録（`examples/arms/dsh-deepseek.yaml`、定義のみ先行。
      実測は prerequisites（Node 24.9・pnpm 11・security audit・ACP 安定）全成立まで blocked）
- [x] ハーネス変更時のリプレイを CI 化: local gate `make ci` = verify + replay
     移植済み: remote `AOI-Future/agent-work-harness`（public）公開、
      `.github/workflows/ci.yml` が push/PR ごとに `make ci` を実行）

## C'. 統合の自動化（human gate 維持）

completed から元リポジトリ反映までの人手パッチ手渡しを、ゲート付き半自動化にする。

- [x] `integrate` 段階追加: `completed -> integrate_ready -> integrated`（`34c4384`、テスト3本追加・計50 OK）
- [x] patch 生成: `integration-patch`（消化用 clean clone で差分凍結 + sha256 receipt）
      統合後状態での受入コマンド再実行は既存の check 群で人手実行可能
- [x] conflict 検出（クリーン clone 適用 test）: r3b で実証（`apply --check` CLEAN +
      適用ツリーで suite rc=0）。境界超過の `waiting_human` 化は済（既存の
      allowed/protected 境界が submit 時に検証済み）
- [x] push / merge / 公開を引き続き構造的に持たない境界として明文化（`mark-integrated` は sha 記録のみ）
- [x] イベントテーブルの追記専用化・再生可能化（dsh の durable event ログ Patterns 移植。
      SQLite トリガーで UPDATE/DELETE を物理拒否 + `replay` は汚損行で失敗。`28fea86`）

## dsh 定点観測チェックリスト（深結合解禁の条件）

- developer preview 表記の外れた正式リリース
- security audit の実施情報
- ACP profile の安定性（session format migrations の落ち着き）
- 我々の seam を通した B' 実測で、同一契約での勝敗データが揃った時点

---

## 追記 2026-11-08（first real E2E の ledger 確定と drift 教訓）

### 判明: 手作業統合は証跡チェーンを切る（C' 自動化の動機が実証された）
- r2 は `completed`（reviewed revision `08006e19…`）に到達したが、人手の Cons①②③
  裁定後の cp→push で作業ツリーが `1b3dcf6e…` にドリフト。
- ハーネスは `completed_revision_changed` を検知し `waiting_human` に降格・証跡を null 化。
  **設計どおり**。review 済みバイト ≠ shipping 済みバイト（`896f301`）を正直に反映。
- 結論: 統合は必ず `integration-patch`（レビュー時リビジョンを再現する使い捨てクローン）
  経由で行い、手作業 cp は使わない。これが C' 自動化を必須とする実証根拠。

### human_close で ledger を正直に確定（escalated / waiting_human → closed）
- r1: `closed`（reason: check argv が unittest を指していた、target は pytest 式、エスカレーションは妥当）
- r2: `closed` + `external_revision: 896f301`。`evidence_passed: False` を**消さずに保持**
  （=pipeline 外 shipping を正直に記録）。 whitewash しないのが要点。
- コミット `2d19da1` feat: human_close（CLI `close <task> --reason [--external-sha]`）。

### 教訓の反映先
- check preflight（intake 必須）: r1 失敗の根拠。`eval_report.py` 実装時に intake へ組み込む。
- 統合は seam 経由: 次の E2E（r3）は `integration-patch → mark-integrated` を自動で通し、
  途切れない証跡チェーンを証拠として残す。

## 追記 2026-11-08（r3b: 途切れない証跡チェーンの実証完了）

### r3 の失敗とアダプタ修正（`d7b3e2a`）
- r3（`webhook-json-pretty`）: ワーカーは正しく実装・suite rc=0 相当だったのに、
  `NEEDS_DECISION` の **substring 判定が散文の言及に誤発火**し verify 前に
  `waiting_human` へエスカレート。かつ **stdout を保存していなかったため事後検証不能**。
- 修正2点:
  1. 拒否判定を **行頭完全一致マーカー**（`NEEDS_DECISION` 単独 / `NEEDS_DECISION: 理由`）へ Tighten。
     誤 ready は controller のクリーンコピー再実行＋独立レビューが担保するので、
     「疑わしきは verify へ」が安全側の設計。
  2. backend の全ターン（成功/失敗）を `<runtime>/executor-logs/<task>.<role>.log`
     へ追記保存。以後、エスカレーションは必ず事後監査可能。
- テスト4本追加（散文言及は発火しない/裸マーカーは発火/stdout永続化/エラー永続化）で **56 OK**。
- r3 は `closed`（reason に誤判定の事実と修正 SHA を正直に記録）。

### r3b: C' 完全ライフサイクルを無傷で貫通
`submit(+preflight rc=0) → 実装1試行 → controller verify rc=0 → 独立レビュー PASS →
integration-patch (sha256 7fddc8e2…, 8535B) → クリーン clone apply --check CLEAN +
適用ツリー suite rc=0 → 人手 push 承認 → push 77a40d9 → mark-integrated (integrated_at 記録)`
- 出荷コミット `77a40d9`（v1.2.0）はレビュー済み patch バイトそのもの。r2 の drift 再発なし。
- `integrated` 状態は期限切れ免除・外部 revision バインドで台帳確定。
- **C' のゴール（途切れない証跡チェーン）実証完了。** 残りは自動化（preflight in submit、
  events 追記専用化、eval_report.py）。

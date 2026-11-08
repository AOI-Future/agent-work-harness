# ローカル作業ハーネス設計

Status: ローカルpilot実装済み。受入検証と独立reviewの結果は[verification.md](verification.md)。

## 目的と責任

依頼 → 実装 → 自動検証 → 独立レビュー → 修正 → 完了を、セッション間で継続する。
controllerが契約と状態を管理し、workerは専用コピーのコードだけを編集する。
元リポジトリ、既存Hermes/Supabase queue、scheduler、共有設定を変更しない。
モデルやproviderを呼び出さないpull方式なので、既存セッションをexecutorにできる。

## Moduleとinterface

`Harness.submit(spec)` は固定されたタスクを登録する。
`Harness.advance(task_id, command)` はclaim/report/verify/review/recover/interventionを処理する。
`Harness.inspect(task_id)` は現在の状態、必要な操作、証拠、介入指標を返す。
CLIはこのinterfaceへのadapter。worker/reviewerにDBやcontroller権限を渡さない。
workspace生成、SQLite transaction、scope検査、期限判定、モデルrouting、証拠生成は内部に置く。

## 状態と不変条件

```text
queued -> ready -> implementing -> verifying -> review_ready -> reviewing -> completed -> integrate_ready -> integrated
                  ^                 |                         |
                  +----修正要求-----+-------------------------+
任意の稼働状態 -> waiting_human / escalated
```

- タスクIDと契約digestによる登録の冪等性。同じIDの別契約は拒否。
- 依存先が完了し、その証拠が現在も有効なときだけclaimできる。
- 一つのclaimに一つのlease。reportは期限内のtokenとroleを一致させる。
- claimごとに別のGitコピーを作る。古いleaseのworkspaceは再利用しない。
- 正本sourceがdirtyなら登録を拒否。実装・reviewのコピーだけを変更する。
- 受入条件と検証コマンドはDBに固定。sourceのprotected_pathsとcontextは変更不可。
- 自動検証前後のrevisionが同一であること。コード変更後の証拠流用を拒否。
- 検証はcontrollerが作るクリーンな別コピーで行い、workerのキャッシュを入力にしない。
- 検証コマンドの実行中はDB transactionを保持しない。期限付き予約とtokenで結果を確定する。
- revisionはGit HEADと非一時ファイル全体のSHA-256。未commit差分も含める。
- reviewerは過去の全実装actorと別。全criterionを確認し、独自コピーを改変しない。
- 完了前に実装コピーを再照合し、上流`verify_completion`も実行する。
- 定義済み再試行回数を超えたらescalated。specifiedはsmallからstrongへ切替。
- 設計判断の要求、期限切れ、外部操作は人に返す。承認を実行するAPIは持たない。
- 完了後に証拠の対象が変更されたらwaiting_humanへ移す。

## 権限

actor名/token/digestはローカル協調情報であり、認証やOS隔離ではない。
controllerと受入コマンドは信頼済みのローカル操作主体。SQLiteはこの主体だけが操作する。
worker/reviewerを起動する既存executorのtool/OS sandboxが実際の権限を強制する。
このpilotのCLIをworkerから直接呼べる環境で敵対的なactorを隔離できるとは主張しない。
受入コマンドはshell展開せず固定argv、最小環境、timeout、出力上限を適用するが、
それだけでネットワークやfilesystemをsandbox化できるわけではない。
外部操作、provider実行、認証付き分散transport、本番接続は別の実装・承認が必要。

## 継承

`aoi-system` commit `79b78ff8dfe945db23dc5e51c45edd1528c66f95`のprotocol coreを
`harness/vendor`へ固定版で保全する。`TaskContract`/`EvidenceReceipt`/canonical digest/
`verify_completion`を最終証拠の検査に使用する。vendorの内容はmanifestで機械検証する。
既存Phase 2のKanban adapterや本番許可を流用しない。
早期の作業契約は`TaskSpec`、成果物のhash確定後にcontrollerが上流の証拠契約へ変換する。
生成した契約に変更後の受入条件を混ぜず、最初のTaskSpec digestをsource referenceに束縛する。

## モデル・評価

役割と難度からsmall/strongを返す。Luna/Sol/Astraの具体選択は既存executor側で行う。
workerとreviewerは別セッション。reviewerは元の依頼から独立して評価する。
実際のモデル別成功率は未測定。過去タスクの評価manifestを登録して同じ条件で比較する。
実装attempt、修正回数、所要時間、モデルtier、完了率、介入回数/秒数を記録する。
費用はexecutorから任意に報告される値を保存し、未報告をゼロと見なさない。
通知対象はcompleted、escalated、waiting_humanへの遷移のみ。進行イベントはjournalで確認する。

## 統合

並列の実装とreviewは分離コピーを使用する。完成した変更はpatchとしてローカルに渡す。
元リポジトリへのapply、共有設定の統合、PR/push/merge、公開は自動実行しない。
統合担当が候補を順番に適用し、統合された状態で再検証する。
cross-repository dependencyや未信頼コード、大規模asset/symlinkはpilotの対象外。

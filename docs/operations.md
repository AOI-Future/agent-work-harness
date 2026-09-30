# 使い方

このリポジトリを作業ディレクトリとしてCLIを呼ぶ。JSON形式は次のコマンドで取得できる。

```bash
python3 -m harness.cli schema --kind task
python3 -m harness.cli schema --kind worker-report
python3 -m harness.cli schema --kind review-report
```

## タスク登録

`examples/task-template.json`をコピーし、cleanなsource_repo、変更範囲、context、
受入コマンドを設定する。コマンドはcontrollerが事前に確認する。受入コードを置く
場所は必ずprotected_pathsに含める。allowed_pathsは正確なfile/directory名で、globは使わない。
runtimeの保存先はsource_repoの外に置く。

```bash
python3 -m harness.cli submit /absolute/path/task.json
python3 -m harness.cli claim example-task --role worker --actor session-implementation
```

controllerが返すJSONをworkerに渡す。workerはlease.workspaceでallowed_pathsだけを変更し、
tokenと`outcome: ready`を持つJSONをcontrollerに返す。判断が必要なら`needs_decision`を返す。
controllerはworkerの最終説明を証拠として採用せず、自動検証を実行する。

```bash
python3 -m harness.cli report example-task /absolute/path/worker-report.json
python3 -m harness.cli verify example-task
python3 -m harness.cli claim example-task --role reviewer --actor session-review
```

過去に実装を担当した全セッションと別のreviewerに新しいpacketを渡す。元の依頼、受入条件、check結果、workspaceの
差分を確認し、全criterionをcovered_criteriaへ列挙する。実装担当の説明だけで判定しない。
reviewerのコピーは変更しない。token、target_revision、verdict、covered_criteria、findingsを返す。
`changes`には再現手順/該当箇所を含む指摘が必要。修正claimは再び別のコピーを作る。

```bash
python3 -m harness.cli review example-task /absolute/path/review-report.json
python3 -m harness.cli inspect example-task
python3 -m harness.cli evidence example-task
```

## 停止・復旧・介入

lease期限後は`recover`を実行する。古いtokenは拒否され、古い作業コピーは引き継がれない。
検証担当が停止した場合も、検証予約の期限後にrecoverする。遅れて返った検証結果は採用しない。
controllerを再起動しても登録、lease、journal、証拠はSQLiteから復元する。
期限切れタスク、判断要求、scope違反はwaiting_human。予算切れはescalated。
このpilotはこれらを自動承認しない。条件を人が再定義した後、別IDの新タスクで再開する。

```bash
python3 -m harness.cli recover example-task
python3 -m harness.cli intervention example-task --seconds 120
python3 -m harness.cli notifications example-task --after 0
```

notificationsのseqをcursorとして次回の`--after`へ渡す。通常の進行を通知しない。
eventsは全journal。費用未報告はnullで、ゼロではない。
small/strongはroutingの推奨tier。実際のLuna/Sol/Astra選択はexecutorに任せる。

## 統合・評価

completedのworkspaceとrevisionを確認してローカル差分を取り出す。
元リポジトリへの反映、PR/push/mergeはcontrollerが勝手に実行しない。
複数タスクの成果を統合した状態でも受入チェックを再実行する。
過去の実タスクは`examples/evaluation-template.json`を基に登録し、モデル設定を固定して
成功率、手戻り、所要時間、介入秒数、費用を比較する。
demoは制御されたfixtureであり、実モデル性能や本番動作の証明には使わない。

## この環境で完了した実タスク

`schema-export-pilot`はこのCLI自身にschema exportを追加した開発タスク。
rootエージェントが実装担当、別セッションのエージェントがreviewerとして参加した。
状態と証拠はsource_repo外の`/Users/shugo/project/.agent-work-harness-pilot`に保持している。

```bash
python3 -m harness.cli --runtime /Users/shugo/project/.agent-work-harness-pilot inspect schema-export-pilot
python3 -m harness.cli --runtime /Users/shugo/project/.agent-work-harness-pilot evidence schema-export-pilot
```

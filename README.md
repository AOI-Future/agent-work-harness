# Agent Work Harness

許可済みのローカルコード開発を、実装・自動検証・独立レビュー・修正・完了まで継続する。
既存のエージェントセッションから使うpull方式。モデルの追加呼び出しや課金設定の変更は行わない。

- [設計](docs/design.md)
- [使い方と運用](docs/operations.md)
- [最初の案・追加アドバイスとの対応](docs/coverage.md)

```bash
make verify
make demo
python3 -m harness.cli --help
```

Python 3.11+、Pydantic 2.12+、Gitが必要。開発チェックにはRuffとMypyを使う。
この環境では既存のPython/Pydantic/Ruffを利用し、Mypyはローカル`.tools`へ配置する。
`TYPE_PY`はキャッシュしたMypy用のPython 3.12 interpreterを指す。別環境では
`make verify TYPE_PY=/absolute/path/to/python RUFF=/absolute/path/to/ruff`で上書きできる。

タスクごと、claimごとにローカルGitコピーを作り、元リポジトリに書き込まない。
controllerがclaim packetをworkerに渡し、返ってきたreportを受理する。
検証はworkerのキャッシュを引き継がない専用コピーで行い、長いコマンド中はDBをロックしない。
全受入コマンドの合格後、別セッションのreview reportで完了を判定する。
期限切れtoken、同じ担当によるレビュー、古いrevisionの証拠は拒否する。

controllerの状態DBは`.runtime/`に保存しGitから除外する。
actor名は認証された本人証明ではない。信頼されたローカルcontrollerと既存executorの
sandboxが前提。本番・外部操作・公開・認証変更・自動mergeの実行機能は持たない。

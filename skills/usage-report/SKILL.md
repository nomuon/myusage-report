---
name: usage-report
description: Claude Code の使用状況レポートを生成。"usage report", "使用状況レポート", "Claude利用分析", "レポート生成" で呼び出し
disable-model-invocation: true
allowed-tools: Bash(python3 *)
argument-hint: "[--days N] [--output path] [--extract-only]"
---

# Claude Code Usage Report Generator

Claude Code のローカル会話履歴を解析し、使用状況をビジュアルな HTML レポートとして生成する。
外部 API・ネットワーク通信なし、完全ローカル処理。

## 実行

以下のコマンドでレポートを生成してください:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/claude_usage_report.py $ARGUMENTS
```

## オプション

| オプション | 説明 | デフォルト |
|:--|:--|:--|
| `--days N` | 直近N日間のデータのみ集計 | 0(全期間) |
| `--output PATH` | 出力ファイルパス | ./report.html |
| `--claude-dir PATH` | Claude設定ディレクトリ | ~/.claude |
| `--extract-only` | データ抽出のみ(JSON出力) | - |

## 実行例

- 全期間のレポート: 引数なしで実行
- 直近30日間: `--days 30`
- JSON出力: `--extract-only`
- 出力先を指定: `--output ~/Desktop/report.html`

## レポート内容

生成される HTML レポート(Chart.js ダークテーマ)には以下が含まれる:

- 時間帯別ヒートマップ
- 曜日別アクティビティ分布
- 日別/週次トレンドグラフ
- モデル別使用量とコスト推定
- セッションカテゴリ分類(デバッグ、実装、リファクタリング等)
- セッション深度分布(クイック〜集中セッション)
- プロジェクト別ランキング
- ツール使用統計

レポート生成後、出力されたパスをブラウザで開いて確認してください。

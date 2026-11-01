# Claude Usage Report

Claude Code のローカル会話履歴（`~/.claude/projects/` 内の JSONL ファイル）を解析し、使用状況をビジュアルな HTML レポートとして生成するツール。

- 外部 API・ネットワーク通信なし、完全ローカル処理
- Python 標準ライブラリのみ使用（追加インストール不要）
- Chart.js ダークテーマのダッシュボード形式で出力

## 機能

- 時間帯別ヒートマップ（ピーク時間帯の可視化）
- 曜日別アクティビティ分布
- 日別・週次トレンドグラフ
- モデル別使用量とコスト推定（Fable / Opus / Sonnet / Haiku 対応、Anthropic API の公開単価で換算）
- セッションカテゴリ分類（デバッグ、実装、リファクタリング等）
- セッション深度分布（クイック〜集中セッション）
- プロジェクト別ランキング
- ツール使用統計

## 必要要件

- Python 3.8+
- Claude Code の会話履歴（`~/.claude/projects/`）

## インストール

### CLI として使う

```bash
git clone https://github.com/nomuon/myusage-report.git
cd myusage-report
python3 claude_usage_report.py
```

### Claude Code Skill として使う

1. リポジトリをクローン:

```bash
git clone https://github.com/nomuon/myusage-report.git
```

2. スキルディレクトリにシンボリックリンクを作成:

```bash
ln -s /absolute/path/to/claude-usage-report/skills/usage-report ~/.claude/skills/usage-report
```

> **Note:** `/absolute/path/to/` はクローン先の絶対パスに置き換えてください。

3. Claude Code で `/usage-report` と入力して使用:

```
/usage-report
/usage-report --days 30
/usage-report --extract-only
```

## 使い方

### CLI

```bash
# 全期間のレポートを生成
python3 claude_usage_report.py

# 直近30日間のみ集計
python3 claude_usage_report.py --days 30

# 出力先を指定
python3 claude_usage_report.py --output ~/Desktop/report.html

# Claude設定ディレクトリを指定
python3 claude_usage_report.py --claude-dir /path/to/.claude

# JSON形式でデータ抽出のみ
python3 claude_usage_report.py --extract-only
```

### Claude Code Skill

```
/usage-report                          # 全期間レポート
/usage-report --days 30                # 直近30日間
/usage-report --output ~/Desktop/report.html   # 出力先指定
/usage-report --extract-only           # JSON出力
```

## オプション一覧

| オプション | 説明 | デフォルト |
|:--|:--|:--|
| `--days N` | 直近N日間のデータのみ集計（0=全期間） | `0` |
| `--output PATH` | 出力ファイルパス | `report.html` |
| `--claude-dir PATH` | Claude設定ディレクトリ | `~/.claude` |
| `--extract-only` | データ抽出のみ（JSON を stdout に出力） | - |

## レポート内容

生成される HTML レポートは以下のセクションで構成されます（日付・時刻はすべて JST）:

- **サマリーカード** - 総セッション数、ピーク時間帯、総コスト等
- **時間帯ヒートマップ** - 24時間の利用パターン
- **曜日別分布** - 週間の利用傾向
- **日別トレンド** - 日別アクティビティの推移
- **週次トレンド** - 週単位の推移
- **モデル使用量** - モデル別のトークン数とコスト
- **カテゴリ分類** - セッションの目的別分類
- **セッション深度** - 会話の長さによる分類（クイック / 標準 / 深掘り / 集中）
- **プロジェクトランキング** - プロジェクト別セッション数

## ディレクトリ構成

```
myusage-report/
├── claude_usage_report.py          # メインスクリプト
├── skills/
│   └── usage-report/
│       ├── SKILL.md                # Claude Code Skill 定義
│       └── scripts/
│           └── claude_usage_report.py  # -> ../../.. へのシンボリックリンク
├── README.md
├── LICENSE
└── .gitignore
```

## License

MIT

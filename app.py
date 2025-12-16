import streamlit as st
import google.generative_ai as genai
import os
import glob

# ページ設定
st.set_page_config(page_title="完全自動・税務AI", layout="wide")

# APIキーの設定（StreamlitのSecretsから読み込む）
# ※あとで設定します
if "GEMINI_API_KEY" in st.secrets:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
else:
    st.error("APIキーが設定されていません。")
    st.stop()

# モデル設定 (無料枠なら flash を推奨)
MODEL_NAME = "gemini-1.5-flash"

def load_data():
    """dataフォルダ内のMarkdownを全部読み込む"""
    all_text = ""
    # dataフォルダ内の全サブフォルダを再帰的に探す場合など調整可能
    # ここではシンプルに dataフォルダ直下 または data/サブフォルダ/*.md を想定
    files = glob.glob("data/**/*.md", recursive=True)
    
    if not files:
        return "データがまだありません。"
    
    for file in files:
        with open(file, "r", encoding="utf-8") as f:
            all_text += f"\n\n--- FILE: {os.path.basename(file)} ---\n"
            all_text += f.read()
    return all_text

st.title("🤖 完全自動・税務AI (Free Edition)")
st.caption("毎週自動更新される国税庁データに基づいています")

# チャット履歴の初期化
if "messages" not in st.session_state:
    st.session_state.messages = []

# データの読み込み（キャッシュしても良いが、シンプルに毎回読む）
context_data = load_data()

# チャット画面の表示
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# ユーザー入力
if prompt := st.chat_input("質問を入力してください（例：修繕費の判断基準は？）"):
    # ユーザーの声を履歴に追加
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # AIの回答生成
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        try:
            # システムプロンプト + データ + ユーザーの質問
            model = genai.GenerativeModel(
                MODEL_NAME,
                system_instruction=f"あなたは税務の専門家AIです。以下の最新データに基づいて回答してください。\n\n【参照データ】\n{context_data}"
            )
            
            # ストリーミングで回答表示
            full_response = ""
            response = model.generate_content(prompt, stream=True)
            for chunk in response:
                if chunk.text:
                    full_response += chunk.text
                    message_placeholder.markdown(full_response + "▌")
            
            message_placeholder.markdown(full_response)
            
            # 履歴に追加
            st.session_state.messages.append({"role": "assistant", "content": full_response})
            
        except Exception as e:
            st.error(f"エラーが発生しました: {e}")

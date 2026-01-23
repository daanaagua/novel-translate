import streamlit as st
import pandas as pd
from src.core.schemas import ChunkStatus

def show_dashboard(project):
    st.title(f"仪表盘: {project.book_id}")
    
    # 获取统计数据
    # 遍历 artifacts/chapters 目录下的 json 文件
    chapter_files = sorted(project.memory.chapters_dir.glob("*.json"))
    
    total_chapters = len(chapter_files)
    total_chunks = 0
    completed_chunks = 0
    total_tokens = 0
    
    chapter_stats = []
    
    # 进度条占位符
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    # 快速扫描
    for i, ch_file in enumerate(chapter_files):
        chapter = project.memory.load_chapter(ch_file.stem)
        if not chapter:
            continue
            
        ch_total = len(chapter.chunks)
        ch_completed = sum(1 for c in chapter.chunks if c.status in [ChunkStatus.COMPLETED, ChunkStatus.HUMAN_REVIEW])
        ch_tokens = sum(c.token_count or 0 for c in chapter.chunks)
        
        total_chunks += ch_total
        completed_chunks += ch_completed
        total_tokens += ch_tokens
        
        chapter_stats.append({
            "Chapter ID": chapter.id,
            "Title": chapter.title,
            "Progress": f"{ch_completed}/{ch_total}",
            "Status": "✅" if ch_completed == ch_total else "🔄" if ch_completed > 0 else "⬜",
            "Tokens": ch_tokens
        })
        
        # 更新加载进度
        progress_bar.progress((i + 1) / total_chapters)
    
    # 移除进度条
    progress_bar.empty()
    
    # 核心指标
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("总章节", total_chapters)
    col2.metric("总分块", total_chunks)
    col3.metric("已完成", f"{completed_chunks} ({completed_chunks/total_chunks*100:.1f}%)" if total_chunks else "0%")
    col4.metric("总词数 (Est. Tokens)", total_tokens)
    
    # 详细列表
    st.subheader("章节列表")
    df = pd.DataFrame(chapter_stats)
    st.dataframe(
        df,
        column_config={
            "Progress": st.column_config.ProgressColumn("进度", min_value=0, max_value=100, format="%f"), # 简化显示
        },
        use_container_width=True
    )

"""
DeepNovel-Translator CLI 入口 (v2.0 Project-Based)
"""
import argparse
import json
import sys
import shutil
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.utils.config_loader import ConfigLoader
from src.core.llm_client import LLMManager
from src.core.translator import TranslationEngine, TranslationConfig
from src.core.project_manager import ProjectManager
from src.agents.glossary_manager import GlossaryManager
from src.agents.logic_analyzer import LogicAnalyzer
from src.core.schemas import ChunkStatus

# 初始化核心组件
config_loader = ConfigLoader(config_dir=str(project_root / "config"))
project_manager = ProjectManager(projects_root=str(project_root / "projects"))

def cmd_init(args):
    """初始化新项目"""
    print(f"正在创建项目: {args.book_id} ...")
    try:
        project = project_manager.create_project(
            book_id=args.book_id,
            source_path=args.file,
            force=args.force
        )
        print(f"✅ 项目创建成功！位置: {project.root_dir}")
        print(f"下一步: 请运行 'python main.py translate {args.book_id}' 开始翻译")
    except Exception as e:
        print(f"❌ 创建失败: {e}")

def cmd_list(args):
    """列出所有项目"""
    projects = project_manager.list_projects()
    if not projects:
        print("暂无项目。请使用 'init' 命令创建。")
        return
    
    print("【项目列表】")
    for p in projects:
        print(f"  - {p}")

def cmd_translate(args):
    """项目翻译命令"""
    book_id = args.book_id
    
    try:
        project = project_manager.load_project(book_id)
    except FileNotFoundError:
        print(f"❌ 项目 '{book_id}' 不存在。")
        return

    # 1. 加载配置
    # TODO: 优先加载项目级配置 project.config_file
    global_config = config_loader.load_config()
    prompts = config_loader.load_prompts()
    
    # 2. 初始化引擎组件
    llm_manager = LLMManager(global_config["llm"])
    
    # 加载项目专用术语表
    glossary_manager = GlossaryManager(str(project.glossary_dir))
    glossary_manager.load()
    
    # 初始化翻译引擎
    trans_config = TranslationConfig(
        enable_logic_analysis=not args.no_logic,
        enable_polish=not args.no_polish,
        draft_temperature=args.temp_draft,
        polish_temperature=args.temp_polish
    )
    engine = TranslationEngine(
        llm_manager=llm_manager,
        glossary_manager=glossary_manager,
        prompts=prompts,
        config=trans_config
    )
    
    print(f"🚀 开始翻译项目: {book_id}")
    print(f"配置: 逻辑分析={'OFF' if args.no_logic else 'ON'}, 润色={'OFF' if args.no_polish else 'ON'}")
    
    # 3. 遍历章节和 Chunk
    # 从文件系统加载章节列表
    chapter_files = sorted(project.memory.chapters_dir.glob("*.json"))
    
    total_processed = 0
    total_skipped = 0
    
    for ch_file in chapter_files:
        chapter_id = ch_file.stem
        
        # 如果指定了章节范围
        if args.chapters and chapter_id not in args.chapters.split(','):
            continue
            
        print(f"\n📖 处理章节: {chapter_id}")
        
        # 加载章节数据
        chapter = project.memory.load_chapter(chapter_id)
        if not chapter:
            print(f"  ⚠️ 无法加载章节 {chapter_id}")
            continue
            
        for chunk in chapter.chunks:
            # 检查是否需要跳过
            if project.memory.should_skip(chunk, force=args.force):
                print(f"  ⏭️ Chunk {chunk.id}: 已完成，跳过")
                total_skipped += 1
                continue
            
            print(f"  ⚡ 翻译 Chunk {chunk.id} (Tokens: {chunk.token_count})...")
            
            # 获取上下文
            context = project.memory.get_context_for_chunk(chunk)
            
            # 执行翻译
            result_chunk = engine.translate_chunk(chunk, previous_summary=context)
            
            # 保存结果 (实时持久化)
            project.memory.save_chunk(result_chunk)
            
            if result_chunk.status == ChunkStatus.COMPLETED:
                print(f"     ✅ 完成")
            else:
                print(f"     ❌ 失败: {result_chunk.error_message}")
            
            total_processed += 1
            
            # 简单的限流/中断检查点
            # time.sleep(0.5) 
            
    print("\n" + "="*40)
    print(f"🎉 任务结束")
    print(f"处理: {total_processed}")
    print(f"跳过: {total_skipped}")

def main():
    parser = argparse.ArgumentParser(
        description="DeepNovel-Translator CLI (v2.0)",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")
    
    # init
    p_init = subparsers.add_parser("init", help="初始化新项目")
    p_init.add_argument("book_id", help="项目ID (英文，无空格)")
    p_init.add_argument("file", help="小说原文路径 (.txt)")
    p_init.add_argument("--force", "-f", action="store_true", help="强制覆盖已存在项目")
    p_init.set_defaults(func=cmd_init)
    
    # list
    p_list = subparsers.add_parser("list", help="列出所有项目")
    p_list.set_defaults(func=cmd_list)
    
    # translate
    p_trans = subparsers.add_parser("translate", help="翻译项目")
    p_trans.add_argument("book_id", help="项目ID")
    p_trans.add_argument("--chapters", "-c", help="指定章节ID，逗号分隔 (如 ch01,ch02)")
    p_trans.add_argument("--force", "-f", action="store_true", help="强制重翻已完成的块")
    p_trans.add_argument("--no-logic", action="store_true", help="禁用逻辑分析")
    p_trans.add_argument("--no-polish", action="store_true", help="禁用润色")
    p_trans.add_argument("--temp-draft", type=float, default=0.1, help="直译温度")
    p_trans.add_argument("--temp-polish", type=float, default=0.7, help="润色温度")
    p_trans.set_defaults(func=cmd_translate)
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        return
    
    args.func(args)

if __name__ == "__main__":
    main()

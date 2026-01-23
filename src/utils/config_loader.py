"""
配置加载工具
"""
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional
import yaml


class ConfigLoader:
    """配置加载器"""
    
    def __init__(self, config_dir: str = "config"):
        self.config_dir = Path(config_dir)
        self._config: Optional[Dict[str, Any]] = None
        self._prompts: Optional[Dict[str, Any]] = None
    
    def load_config(self, filename: str = "config.yaml") -> Dict[str, Any]:
        """
        加载主配置文件
        
        Args:
            filename: 配置文件名
        
        Returns:
            配置字典
        """
        config_path = self.config_dir / filename
        
        if not config_path.exists():
            # 尝试加载示例配置
            example_path = self.config_dir / "config.example.yaml"
            if example_path.exists():
                config_path = example_path
            else:
                raise FileNotFoundError(f"配置文件不存在: {config_path}")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 解析环境变量
        config = self._resolve_env_vars(config)
        
        self._config = config
        return config
    
    def load_prompts(self, filename: str = "prompts.yaml") -> Dict[str, Any]:
        """
        加载 Prompt 配置
        
        Args:
            filename: Prompt 配置文件名
        
        Returns:
            Prompt 配置字典
        """
        prompts_path = self.config_dir / filename
        
        if not prompts_path.exists():
            raise FileNotFoundError(f"Prompt 配置文件不存在: {prompts_path}")
        
        with open(prompts_path, 'r', encoding='utf-8') as f:
            prompts = yaml.safe_load(f)
        
        self._prompts = prompts
        return prompts
    
    def _resolve_env_vars(self, obj: Any) -> Any:
        """递归解析配置中的环境变量"""
        if isinstance(obj, str):
            # 匹配 ${VAR_NAME} 格式
            pattern = r'\$\{([^}]+)\}'
            matches = re.findall(pattern, obj)
            for var_name in matches:
                env_value = os.getenv(var_name, "")
                obj = obj.replace(f"${{{var_name}}}", env_value)
            return obj
        elif isinstance(obj, dict):
            return {k: self._resolve_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_env_vars(item) for item in obj]
        return obj
    
    @property
    def config(self) -> Dict[str, Any]:
        """获取已加载的配置"""
        if self._config is None:
            self.load_config()
        return self._config
    
    @property
    def prompts(self) -> Dict[str, Any]:
        """获取已加载的 Prompt 配置"""
        if self._prompts is None:
            self.load_prompts()
        return self._prompts

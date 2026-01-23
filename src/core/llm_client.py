"""
LLM 客户端工厂模块
支持多渠道模型切换：豆包、OpenAI、Anthropic、DeepSeek 等
"""
import os
import json
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from openai import OpenAI


class BaseLLMClient(ABC):
    """LLM 客户端基类"""
    
    def __init__(self, base_url: str, api_key: str, default_model: str):
        self.base_url = base_url
        self.api_key = self._resolve_api_key(api_key)
        self.default_model = default_model
    
    def _resolve_api_key(self, api_key: str) -> str:
        """解析 API Key，支持从环境变量读取"""
        if api_key.startswith("${") and api_key.endswith("}"):
            env_var = api_key[2:-1]
            resolved = os.getenv(env_var)
            if not resolved:
                raise ValueError(f"环境变量 {env_var} 未设置")
            return resolved
        return api_key
    
    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False
    ) -> str:
        """发送聊天请求"""
        pass
    
    @abstractmethod
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """发送聊天请求并返回 JSON"""
        pass


class OpenAICompatibleClient(BaseLLMClient):
    """
    兼容 OpenAI API 格式的客户端
    适用于：OpenAI、豆包、DeepSeek、Ollama、vLLM 等
    """
    
    def __init__(self, base_url: str, api_key: str, default_model: str):
        super().__init__(base_url, api_key, default_model)
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key
        )
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False
    ) -> str:
        """发送聊天请求，返回文本"""
        kwargs = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        
        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """发送聊天请求，返回解析后的 JSON"""
        result = self.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True
        )
        return json.loads(result)


class AnthropicClient(BaseLLMClient):
    """
    Anthropic Claude 客户端
    注意：Claude 的 API 格式与 OpenAI 不同，需要单独处理
    """
    
    def __init__(self, base_url: str, api_key: str, default_model: str):
        super().__init__(base_url, api_key, default_model)
        # 延迟导入，避免未安装时报错
        try:
            from anthropic import Anthropic
            self.client = Anthropic(api_key=self.api_key)
        except ImportError:
            raise ImportError("请安装 anthropic 库: pip install anthropic")
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False
    ) -> str:
        """发送聊天请求"""
        # 分离 system 消息
        system_msg = ""
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                chat_messages.append(msg)
        
        response = self.client.messages.create(
            model=model or self.default_model,
            max_tokens=max_tokens,
            system=system_msg,
            messages=chat_messages
        )
        return response.content[0].text
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """发送聊天请求，返回 JSON"""
        # Claude 需要在 prompt 中明确要求 JSON
        result = self.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens
        )
        # 尝试提取 JSON
        return json.loads(result)


class LLMClientFactory:
    """
    LLM 客户端工厂
    根据配置创建对应的客户端实例
    """
    
    # 使用 OpenAI 兼容格式的 Provider
    OPENAI_COMPATIBLE = {"doubao", "openai", "deepseek", "custom"}
    
    @classmethod
    def create(
        cls,
        provider: str,
        base_url: str,
        api_key: str,
        default_model: str
    ) -> BaseLLMClient:
        """
        创建 LLM 客户端
        
        Args:
            provider: 服务商名称 (doubao, openai, anthropic, deepseek, custom)
            base_url: API 地址
            api_key: API Key (支持 ${ENV_VAR} 格式)
            default_model: 默认模型 ID
        
        Returns:
            对应的 LLM 客户端实例
        """
        if provider in cls.OPENAI_COMPATIBLE:
            return OpenAICompatibleClient(base_url, api_key, default_model)
        elif provider == "anthropic":
            return AnthropicClient(base_url, api_key, default_model)
        else:
            # 未知 provider，尝试用 OpenAI 兼容格式
            return OpenAICompatibleClient(base_url, api_key, default_model)


class LLMManager:
    """
    LLM 管理器
    统一管理多个模型实例，支持按用途（logic/draft/polish）调用不同模型
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化 LLM 管理器
        
        Args:
            config: 从 config.yaml 读取的 llm 配置段
        """
        self.config = config
        self.active_provider = config["active_provider"]
        self.provider_config = config["providers"][self.active_provider]
        
        # 创建客户端（所有用途共用一个客户端，通过 model 参数区分）
        self.client = LLMClientFactory.create(
            provider=self.active_provider,
            base_url=self.provider_config["base_url"],
            api_key=self.provider_config["api_key"],
            default_model=self.provider_config["models"]["draft"]
        )
        
        # 模型映射
        self.models = self.provider_config["models"]
    
    def get_model(self, purpose: str) -> str:
        """获取指定用途的模型 ID"""
        return self.models.get(purpose, self.models["draft"])
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        purpose: str = "draft",
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
        json_mode: bool = False
    ) -> str:
        """
        发送聊天请求
        
        Args:
            messages: 消息列表
            purpose: 用途 (logic, draft, polish)
            temperature: 温度，None 则使用默认值
            max_tokens: 最大 Token 数
            json_mode: 是否强制 JSON 输出
        """
        model = self.get_model(purpose)
        temp = temperature if temperature is not None else (0.1 if purpose == "logic" else 0.7)
        
        return self.client.chat(
            messages=messages,
            model=model,
            temperature=temp,
            max_tokens=max_tokens,
            json_mode=json_mode
        )
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        purpose: str = "logic",
        temperature: float = 0.1,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """发送聊天请求，返回 JSON"""
        model = self.get_model(purpose)
        return self.client.chat_json(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens
        )

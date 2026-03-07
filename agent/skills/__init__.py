import importlib.util
from pathlib import Path
from typing import Dict, Any

SKILLS_DIR = Path(__file__).parent
CORE_DIR = SKILLS_DIR / "core"
DYNAMIC_DIR = SKILLS_DIR / "dynamic"

_registry = {}

def load_skills() -> Dict[str, Any]:
    """
    Import all .py files from both core/ and dynamic/ directories.
    Core skills are read-only (shipped with app).
    Dynamic skills are TC-created or user-added.
    
    Returns dict mapping skill_name -> module
    """
    _registry.clear()
    
    # Load core skills first (read-only)
    if CORE_DIR.exists():
        for path in CORE_DIR.glob("*.py"):
            name = path.stem
            if name.startswith("_"):
                continue
            
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                
                # Verify module has NAME attribute
                if hasattr(module, 'NAME'):
                    _registry[name] = module
                    print(f"✅ Loaded skill: {name} (core)")
                else:
                    print(f"⚠️  Skipped {name}: Missing NAME variable")
            except Exception as e:
                print(f"⚠️  Failed to load skill {name}: {e}")
    
    # Load dynamic skills (can be created/modified by TC)
    if DYNAMIC_DIR.exists():
        for path in DYNAMIC_DIR.glob("*.py"):
            name = path.stem
            if name.startswith("_"):
                continue
            
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                
                if hasattr(module, 'NAME'):
                    _registry[name] = module
                    print(f"✅ Loaded skill: {name} (dynamic)")
                else:
                    print(f"⚠️  Skipped {name}: Missing NAME variable")
            except Exception as e:
                print(f"⚠️  Failed to load skill {name}: {e}")
    
    return _registry

def get_skill(name: str) -> Any:
    """Get a skill module by name"""
    return _registry.get(name)

def list_skills() -> Dict[str, str]:
    """Return dict of {skill_name: description}"""
    skills = {}
    for name, module in _registry.items():
        doc = getattr(module, 'DOC', 'No description')
        skills[name] = doc
    return skills

def list_functions(skill_name: str) -> list:
    """List callable functions in a skill (excludes imports)"""
    if skill_name not in _registry:
        return []
    
    module = _registry[skill_name]
    functions = []
    
    for name in dir(module):
        if name.startswith('_'):
            continue
        obj = getattr(module, name, None)
        if callable(obj) and name.islower():  # Only lowercase = real functions
            functions.append(name)
    
    return functions

def call_skill(skill_name: str, func_name: str, *args, **kwargs) -> Any:
    """
    Safely call a skill function.
    Example: call_skill('sys', 'ls', '/tmp')
    """
    if skill_name not in _registry:
        raise ValueError(f"Skill '{skill_name}' not found")
    
    module = _registry[skill_name]
    if not hasattr(module, func_name):
        raise ValueError(f"Function '{func_name}' not found in skill '{skill_name}'")
    
    func = getattr(module, func_name)
    if not callable(func):
        raise ValueError(f"'{func_name}' is not callable")
    
    return func(*args, **kwargs)
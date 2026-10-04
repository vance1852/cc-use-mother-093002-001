from setuptools import find_packages, setup

setup(
    name="digital-trade-foundation",
    version="0.2.0",
    description="跨国数字贸易会谈编排协作服务",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)

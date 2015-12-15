from setuptools import setup, find_packages


setup(
    name = "zkmq",
    version = "0.2",
    packages = find_packages(),
    install_requires = [
        'zc-zookeeper-static == 3.4.4'
    ]
)

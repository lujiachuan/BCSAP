# 部署打包脚手架：PyInstaller spec 与构建脚本
#
# 目录说明：
#   spectrum_client.spec            桌面客户端（GUI，无控制台，onedir）
#   spectrum_data_service.spec      共享数据服务（控制台，onedir）
#   spectrum_instrument_service.spec 仪器执行服务（控制台，onedir）
#   build.ps1                       一键构建脚本
#
# 产物输出：
#   dist/<name>/<name>.exe   可运行文件夹版（分发给用户前建议再套 Inno Setup）
#   build/                   PyInstaller 中间产物，可随时删除
#
# 详见仓库根目录 deploy/README.md。

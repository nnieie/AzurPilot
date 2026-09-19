// 实例名会成为配置文件名，字符集必须避开路径分隔符与系统保留字符：
// 首字符为字母或汉字，其余为字母、数字、汉字、短横线或下划线。
// 汉字范围写成转义序列，避免肉眼无法分辨的兼容区字形被写错码点。
const HAN = '\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff'

// 短横线写两个反斜杠，让 `\-` 原样传给 RegExp：HTML pattern 按 v 标志编译，
// 字符类里未转义的短横线是语法错误，浏览器会直接忽略整个 pattern 属性。
export const INSTANCE_NAME_PATTERN = `[A-Za-z${HAN}][A-Za-z0-9_${HAN}\\-]{0,63}`

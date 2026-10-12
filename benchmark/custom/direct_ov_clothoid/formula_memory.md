# G1 Hermite 回旋线公式卡 v1

公开方法：E. Bertolazzi, M. Frego, *Fast and accurate G1 fitting of
clothoid curves*, arXiv:1305.6644。适用触发词：G1 clothoid、单段回旋线、
两端点与两端切向拟合、道路线形。本文不含任何测试题数值或 worked example。

## 输入约定

内部采用数学平面 `(x, y)` 与从 `+x` 轴逆时针的弧度角 `theta`。若输入是
道路测量坐标 `(E, N)` 和“从北起顺时针”的方位角 `azimuth`（degree）：

```text
x = E
y = N
theta = pi/2 - radians(azimuth)
```

先把终点角展开为与起点角最近的等价角。角差归一化函数应返回
`[-pi, pi)`。

## Canonical 单段 G1 解

给定 `(x0, y0, theta0)` 与 `(x1, y1, theta1)`：

```text
dx = x1 - x0                         dy = y1 - y0
r = hypot(dx, dy)                    phi = atan2(dy, dx)
phi0 = normalize(theta0 - phi)       phi1 = normalize(theta1 - phi)
delta = phi1 - phi0
```

定义归一化弧长变量 `z in [0,1]`：

```text
phase(z; A) = A*z^2 + (delta-A)*z + phi0
g(A)  = integral_0^1 sin(phase(z; A)) dz
g'(A) = integral_0^1 cos(phase(z; A))*(z^2-z) dz
```

求论文选定的 canonical 根 `g(A)=0`。普通分支可从
`A0 = 3*(phi0+phi1)` 开始 Newton：

```text
A <- A - g(A)/g'(A)
```

每步必须检查有限值与非零导数；达到 `abs(g)<=1e-12` 或 100 步停止，否则
拒绝输出。随后：

```text
h       = integral_0^1 cos(phase(z; A)) dz
L       = r/h
kappa0  = (delta-A)/L
dkappa  = 2*A/L^2
kappa1  = kappa0 + dkappa*L
```

只接受 `L>0` 的无绕环 canonical 解。曲线在真实弧长 `s` 处为：

```text
theta(s) = theta0 + kappa0*s + 0.5*dkappa*s^2
x(s) = x0 + integral_0^s cos(theta(u)) du
y(s) = y0 + integral_0^s sin(theta(u)) du
```

任意比例桩点使用 `s=f*L` 回代。道路方位角输出为
`(90-degrees(theta(s))) mod 360`。

## 强制校核

用与求根积分调用分开的终点积分回代，报告：

```text
position_residual = hypot(x(L)-x1, y(L)-y1)
heading_residual  = abs(normalize(theta(L)-theta1))
newton_residual   = abs(g(A))
```

不得把“Newton 收敛”代替端点位置与方向校核；单位必须随结果显式输出。

来源：论文 https://arxiv.org/abs/1305.6644 ；作者参考实现
https://github.com/ebertolazzi/G1fitting 。本公式卡是面向检索的独立摘要。

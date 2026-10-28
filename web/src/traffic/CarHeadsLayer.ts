// The cars themselves: a glowing dot at each car's position, computed on the GPU from `currentTime`.
//
// Every path segment is one instance (from, to, [t0, t1]). The vertex shader keeps only the instance whose time
// span contains currentTime and places a camera-facing quad at the interpolated position; everything else is moved
// outside clip space. Playback changes one uniform per frame, never the data. This complements TripsLayer, whose
// fading trail disappears for a car that has been stopped longer than the trail (e.g. at a red light).
import { type DefaultProps, Layer, type LayerProps, project32, type UpdateParameters } from "@deck.gl/core";
import { Geometry, Model } from "@luma.gl/engine";
import type { ShaderModule } from "@luma.gl/shadertools";

type Vec4 = [number, number, number, number];

export type CarHeadsUniformProps = {
  currentTime: number;
  radiusMeters: number;
  minPixels: number;
  maxPixels: number;
  haloScale: number;
  haloAlpha: number;
  ring: number;
  color: Vec4;
  coreColor: Vec4;
};

const uniformBlock = /* glsl */ `\
layout(std140) uniform carHeadsUniforms {
  float currentTime;
  float radiusMeters;
  float minPixels;
  float maxPixels;
  float haloScale;
  float haloAlpha;
  float ring;
  vec4 color;
  vec4 coreColor;
} carHeads;
`;

export const carHeadsUniforms = {
  name: "carHeads",
  vs: uniformBlock,
  fs: uniformBlock,
  uniformTypes: {
    currentTime: "f32",
    radiusMeters: "f32",
    minPixels: "f32",
    maxPixels: "f32",
    haloScale: "f32",
    haloAlpha: "f32",
    ring: "f32",
    color: "vec4<f32>",
    coreColor: "vec4<f32>",
  },
} as const satisfies ShaderModule<CarHeadsUniformProps>;

const vs = /* glsl */ `\
#version 300 es
#define SHADER_NAME car-heads-layer-vertex-shader

in vec3 positions;
in vec3 instanceFrom;
in vec3 instanceFrom64Low;
in vec3 instanceTo;
in vec3 instanceTo64Low;
in vec2 instanceTimes;

out vec2 unitPosition;
out float coreFrac;

void main(void) {
  float t0 = instanceTimes.x;
  float t1 = instanceTimes.y;
  if (carHeads.currentTime < t0 || carHeads.currentTime >= t1) {
    // Not on this segment now: outside clip space, so the quad is culled.
    gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    return;
  }
  float f = (carHeads.currentTime - t0) / max(t1 - t0, 1e-6);
  vec3 pos = mix(instanceFrom, instanceTo, f);
  vec3 pos64Low = mix(instanceFrom64Low, instanceTo64Low, f);
  geometry.worldPosition = pos;

  float corePx = clamp(project_size_to_pixel(carHeads.radiusMeters), carHeads.minPixels, carHeads.maxPixels);
  float outerPx = corePx * carHeads.haloScale;
  coreFrac = 1.0 / carHeads.haloScale;
  unitPosition = positions.xy;

  gl_Position = project_position_to_clipspace(pos, pos64Low, vec3(0.0), geometry.position);
  gl_Position.xy += project_pixel_size_to_clipspace(positions.xy * outerPx);
}
`;

const fs = /* glsl */ `\
#version 300 es
#define SHADER_NAME car-heads-layer-fragment-shader
precision highp float;

in vec2 unitPosition;
in float coreFrac;
out vec4 fragColor;

void main(void) {
  float d = length(unitPosition);
  if (d > 1.0) discard;
  // Solid hot core (anti-aliased edge), then a soft quadratic halo out to the quad edge. With ring = 1 the core is
  // hollow (a bright ring around a dark centre): the X-ray mark for cars in tunnels.
  float x = d / coreFrac;
  float core = (1.0 - smoothstep(0.65, 1.0, x)) * mix(1.0, smoothstep(0.3, 0.55, x), carHeads.ring);
  float halo = (1.0 - d) * (1.0 - d) * carHeads.haloAlpha;
  vec3 rgb = mix(carHeads.color.rgb, carHeads.coreColor.rgb, core);
  fragColor = vec4(rgb, max(core, halo) * carHeads.color.a);
}
`;

/** Binary per-segment data, as built by prepare.ts (TripGroup.heads). */
export interface CarHeadsData {
  length: number;
  attributes: {
    getFrom: { value: Float64Array; size: 3 };
    getTo: { value: Float64Array; size: 3 };
    getTimes: { value: Float32Array; size: 2 };
  };
}

type _CarHeadsLayerProps = {
  data: CarHeadsData;
  currentTime: number;
  radiusMeters: number;
  minPixels: number;
  maxPixels: number;
  haloScale: number;
  haloAlpha: number;
  /** 0 = solid hot core, 1 = hollow ring (X-ray tunnel cars). */
  ring: number;
  /** RGBA 0–255: halo / rim colour (alpha scales everything). */
  color: Vec4;
  /** RGB(A) 0–255: hot centre colour. */
  coreColor: Vec4;
};

export type CarHeadsLayerProps = _CarHeadsLayerProps & LayerProps;

const defaultProps: DefaultProps<CarHeadsLayerProps> = {
  currentTime: { type: "number", value: 0 },
  radiusMeters: { type: "number", value: 2, min: 0 },
  minPixels: { type: "number", value: 1.5, min: 0 },
  maxPixels: { type: "number", value: 8, min: 0 },
  haloScale: { type: "number", value: 3, min: 1 },
  haloAlpha: { type: "number", value: 0.4, min: 0 },
  ring: { type: "number", value: 0, min: 0, max: 1 },
  color: { type: "color", value: [56, 140, 255, 255] },
  coreColor: { type: "color", value: [200, 225, 255, 255] },
};

const norm = (c: readonly number[]): Vec4 => [c[0]! / 255, c[1]! / 255, c[2]! / 255, (c[3] ?? 255) / 255];

export class CarHeadsLayer extends Layer<Required<_CarHeadsLayerProps> & LayerProps> {
  static layerName = "CarHeadsLayer";
  static defaultProps = defaultProps;

  declare state: { model?: Model };

  getShaders() {
    return super.getShaders({ vs, fs, modules: [project32, carHeadsUniforms] });
  }

  initializeState() {
    this.getAttributeManager()!.addInstanced({
      instanceFrom: { size: 3, type: "float64", fp64: this.use64bitPositions(), accessor: "getFrom" },
      instanceTo: { size: 3, type: "float64", fp64: this.use64bitPositions(), accessor: "getTo" },
      instanceTimes: { size: 2, accessor: "getTimes" },
    });
  }

  updateState(params: UpdateParameters<this>) {
    super.updateState(params);
    if (params.changeFlags.extensionsChanged || !this.state.model) {
      this.state.model?.destroy();
      this.state.model = this._getModel();
      this.getAttributeManager()!.invalidateAll();
    }
  }

  draw() {
    const p = this.props;
    const props: CarHeadsUniformProps = {
      currentTime: p.currentTime,
      radiusMeters: p.radiusMeters,
      minPixels: p.minPixels,
      maxPixels: p.maxPixels,
      haloScale: p.haloScale,
      haloAlpha: p.haloAlpha,
      ring: p.ring,
      color: norm(p.color),
      coreColor: norm(p.coreColor),
    };
    const model = this.state.model!;
    model.shaderInputs.setProps({ carHeads: props });
    model.draw(this.context.renderPass);
  }

  protected _getModel(): Model {
    return new Model(this.context.device, {
      ...this.getShaders(),
      id: this.props.id,
      bufferLayout: this.getAttributeManager()!.getBufferLayouts(),
      geometry: new Geometry({
        topology: "triangle-strip",
        attributes: { positions: { size: 3, value: new Float32Array([-1, -1, 0, 1, -1, 0, -1, 1, 0, 1, 1, 0]) } },
      }),
      isInstanced: true,
    });
  }
}

// ---------------------------------------------------------------------------------------------------------------
// Tethers: a thin vertical line from each tunnel car up to the surface (z = 0), fading towards the top, so a car
// in a tunnel visibly hangs below the street or river it passes under. Same per-segment data and time logic as the
// heads (palette XRAY.tetherAlpha). It is given only the tunnel cars' heads (prepare.ts decides which segments are in
// a tunnel), and draws every one of them, so a car has a tether exactly when it has a ring head and a surface ring.

export type CarTethersUniformProps = { currentTime: number; widthPixels: number; color: Vec4 };

const tetherBlock = /* glsl */ `\
layout(std140) uniform carTethersUniforms {
  float currentTime;
  float widthPixels;
  vec4 color;
} carTethers;
`;

export const carTethersUniforms = {
  name: "carTethers",
  vs: tetherBlock,
  fs: tetherBlock,
  uniformTypes: { currentTime: "f32", widthPixels: "f32", color: "vec4<f32>" },
} as const satisfies ShaderModule<CarTethersUniformProps>;

const tetherVs = /* glsl */ `\
#version 300 es
#define SHADER_NAME car-tethers-layer-vertex-shader

in vec3 positions;
in vec3 instanceFrom;
in vec3 instanceFrom64Low;
in vec3 instanceTo;
in vec3 instanceTo64Low;
in vec2 instanceTimes;

out float vUp;

void main(void) {
  float t0 = instanceTimes.x;
  float t1 = instanceTimes.y;
  if (carTethers.currentTime < t0 || carTethers.currentTime >= t1) {
    gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    return;
  }
  float f = (carTethers.currentTime - t0) / max(t1 - t0, 1e-6);
  vec3 pos = mix(instanceFrom, instanceTo, f);
  vec3 pos64Low = mix(instanceFrom64Low, instanceTo64Low, f);
  // positions.y: 0 at the car, 1 at the surface straight above it.
  vUp = positions.y;
  vec3 p = vec3(pos.xy, mix(pos.z, 0.0, positions.y));
  vec3 p64Low = vec3(pos64Low.xy, mix(pos64Low.z, 0.0, positions.y));
  geometry.worldPosition = p;
  gl_Position = project_position_to_clipspace(p, p64Low, vec3(0.0), geometry.position);
  gl_Position.xy += project_pixel_size_to_clipspace(vec2(positions.x * carTethers.widthPixels * 0.5, 0.0));
}
`;

const tetherFs = /* glsl */ `\
#version 300 es
#define SHADER_NAME car-tethers-layer-fragment-shader
precision highp float;

in float vUp;
out vec4 fragColor;

void main(void) {
  // Brightest at the car, fading to a faint tick at the surface.
  float a = mix(1.0, 0.15, vUp);
  fragColor = vec4(carTethers.color.rgb, carTethers.color.a * a);
}
`;

type _CarTethersLayerProps = {
  data: CarHeadsData;
  currentTime: number;
  widthPixels: number;
  /** RGBA 0–255. */
  color: Vec4;
};

export type CarTethersLayerProps = _CarTethersLayerProps & LayerProps;

const tetherDefaults: DefaultProps<CarTethersLayerProps> = {
  currentTime: { type: "number", value: 0 },
  widthPixels: { type: "number", value: 1.5, min: 0 },
  color: { type: "color", value: [56, 140, 255, 110] },
};

export class CarTethersLayer extends Layer<Required<_CarTethersLayerProps> & LayerProps> {
  static layerName = "CarTethersLayer";
  static defaultProps = tetherDefaults;

  declare state: { model?: Model };

  getShaders() {
    return super.getShaders({ vs: tetherVs, fs: tetherFs, modules: [project32, carTethersUniforms] });
  }

  initializeState() {
    this.getAttributeManager()!.addInstanced({
      instanceFrom: { size: 3, type: "float64", fp64: this.use64bitPositions(), accessor: "getFrom" },
      instanceTo: { size: 3, type: "float64", fp64: this.use64bitPositions(), accessor: "getTo" },
      instanceTimes: { size: 2, accessor: "getTimes" },
    });
  }

  updateState(params: UpdateParameters<this>) {
    super.updateState(params);
    if (params.changeFlags.extensionsChanged || !this.state.model) {
      this.state.model?.destroy();
      this.state.model = new Model(this.context.device, {
        ...this.getShaders(),
        id: this.props.id,
        bufferLayout: this.getAttributeManager()!.getBufferLayouts(),
        geometry: new Geometry({
          topology: "triangle-strip",
          attributes: { positions: { size: 3, value: new Float32Array([-1, 0, 0, 1, 0, 0, -1, 1, 0, 1, 1, 0]) } },
        }),
        isInstanced: true,
      });
      this.getAttributeManager()!.invalidateAll();
    }
  }

  draw() {
    const p = this.props;
    const model = this.state.model!;
    model.shaderInputs.setProps({ carTethers: { currentTime: p.currentTime, widthPixels: p.widthPixels, color: norm(p.color) } });
    model.draw(this.context.renderPass);
  }
}

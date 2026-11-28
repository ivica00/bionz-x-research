// Imports a flat BBC/ORIL av-cam.bin into a Ghidra raw program and lays out the
// measured regions. Run as a -postScript after BinaryLoader has placed the whole
// file at the ORIL base address.
//
//   AvcamOrilImport.java <regions.json> <functions.csv> <out.json> <maxFunctions> <decompile.csv>
//
// The script refuses a program whose language is not ARM:LE:32:v7 or whose image
// base differs from the declared base. It replaces the single loader block with
// one block per measured region (permissions from the JSON), creates up to
// <maxFunctions> Thumb functions from the index CSV, and decompiles the listed
// addresses so the receipt proves a by-address decompile works on the raw image.
//
// Everything it writes is derived from the input bytes; it does not run global
// auto-analysis and does not invent symbols.
// @category A6400.Research
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.framework.Application;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Program;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import java.io.Reader;
import java.io.Writer;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;

public class AvcamOrilImport extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 5) {
            throw new IllegalArgumentException(
                "Expected regions JSON, functions CSV, output JSON, max functions, decompile CSV");
        }
        Path regionsPath = Path.of(args[0]);
        Path functionsPath = Path.of(args[1]);
        Path outPath = Path.of(args[2]);
        int maxFunctions = Integer.parseInt(args[3]);
        Path decompilePath = Path.of(args[4]);
        if (Files.exists(outPath)) {
            throw new IllegalArgumentException("Refusing to overwrite an existing import receipt");
        }

        JsonObject regions;
        try (Reader reader = Files.newBufferedReader(regionsPath, StandardCharsets.UTF_8)) {
            regions = JsonParser.parseReader(reader).getAsJsonObject();
        }
        long base = Long.decode(regions.get("base").getAsString());
        long declaredSize = regions.get("size").getAsLong();

        if (!currentProgram.getLanguageID().toString().equals("ARM:LE:32:v7")) {
            throw new IllegalArgumentException("Expected language ARM:LE:32:v7, got "
                + currentProgram.getLanguageID());
        }

        Memory memory = currentProgram.getMemory();
        MemoryBlock[] existing = memory.getBlocks();
        if (existing.length != 1) {
            throw new IllegalArgumentException("Expected the single BinaryLoader block, found "
                + existing.length);
        }
        MemoryBlock loader = existing[0];
        if (loader.getStart().getOffset() != base) {
            throw new IllegalArgumentException("Expected loader block at " + Long.toHexString(base)
                + ", got " + loader.getStart());
        }
        if (loader.getSize() != declaredSize) {
            throw new IllegalArgumentException("Loader block size " + loader.getSize()
                + " differs from declared " + declaredSize);
        }
        byte[] all = new byte[(int) loader.getSize()];
        loader.getBytes(loader.getStart(), all);
        String sha256 = HexFormat.of().formatHex(
            MessageDigest.getInstance("SHA-256").digest(all));
        // The raw loader leaves the program image base at 0; the ORIL mapping base
        // is the address every index address is expressed against, so record it.
        currentProgram.setImageBase(toAddr(base), false);
        memory.removeBlock(loader, monitor);

        JsonArray blockReport = new JsonArray();
        JsonArray regionArray = regions.getAsJsonArray("regions");
        int index = 0;
        for (var element : regionArray) {
            JsonObject region = element.getAsJsonObject();
            long start = Long.decode(region.get("start_va").getAsString());
            int size = region.get("size").getAsInt();
            String name = String.format("%02d_%s", index++, region.get("kind").getAsString());
            byte[] slice = Arrays.copyOfRange(all, (int) (start - base), (int) (start - base) + size);
            MemoryBlock block = createMemoryBlock(name, toAddr(start), slice, false);
            block.setRead(region.get("read").getAsBoolean());
            block.setWrite(region.get("write").getAsBoolean());
            block.setExecute(region.get("execute").getAsBoolean());
            JsonObject entry = new JsonObject();
            entry.addProperty("name", name);
            entry.addProperty("start", "0x" + Long.toHexString(start));
            entry.addProperty("size", size);
            entry.addProperty("execute", region.get("execute").getAsBoolean());
            blockReport.add(entry);
        }

        Register tmode = currentProgram.getRegister("TMode");
        if (tmode == null) {
            throw new IllegalArgumentException("Program has no TMode context register");
        }
        List<Long> starts = new ArrayList<>();
        List<Long> bounds = new ArrayList<>();
        for (String line : Files.readAllLines(functionsPath, StandardCharsets.UTF_8)) {
            line = line.strip();
            if (line.isEmpty() || line.startsWith("#")) {
                continue;
            }
            String[] parts = line.split(",");
            if (starts.size() >= maxFunctions) {
                break;
            }
            starts.add(Long.decode(parts[0]));
            bounds.add(parts.length > 1 ? Math.min(4096, Long.decode(parts[1])) : 1024L);
        }
        int created = 0;
        int disassembled = 0;
        for (int position = 0; position < starts.size(); position++) {
            long start = starts.get(position);
            Address entry = toAddr(start);
            // Bounded to the index's next-start distance: a longer window would decode
            // a neighbour's literal pool as code and makes TMode context changes conflict.
            long bound = Math.max(2, Math.min(bounds.get(position),
                declaredSize - (start - base)));
            Address end = entry.add(bound - 1);
            if (getInstructionAt(entry) != null) {
                clearListing(entry, end);
            }
            currentProgram.getProgramContext().setValue(tmode, entry, end, BigInteger.ONE);
            DisassembleCommand command =
                new DisassembleCommand(entry, new AddressSet(entry, end), true);
            if (command.applyTo(currentProgram, monitor)) {
                disassembled++;
            }
            if (getFunctionAt(entry) == null) {
                Function function = createFunction(entry, "sub_" + Long.toHexString(start));
                if (function != null) {
                    created++;
                }
            }
        }

        JsonArray decompiled = new JsonArray();
        DecompInterface decompiler = new DecompInterface();
        try {
            if (!decompiler.openProgram(currentProgram)) {
                throw new IllegalStateException("Decompiler could not open the imported program");
            }
            for (String line : Files.readAllLines(decompilePath, StandardCharsets.UTF_8)) {
                line = line.strip();
                if (line.isEmpty() || line.startsWith("#")) {
                    continue;
                }
                long value = Long.decode(line.split(",")[0]);
                Address entry = toAddr(value);
                Function function = getFunctionAt(entry);
                JsonObject record = new JsonObject();
                record.addProperty("address", "0x" + Long.toHexString(value));
                record.addProperty("function", function == null ? "" : function.getName());
                if (function == null) {
                    record.addProperty("status", "no-function");
                    decompiled.add(record);
                    continue;
                }
                DecompileResults results = decompiler.decompileFunction(function, 60, monitor);
                record.addProperty("completed", results.decompileCompleted());
                if (results.decompileCompleted()) {
                    String c = results.getDecompiledFunction().getC();
                    record.addProperty("c_lines", c.split("\n").length);
                    record.addProperty("c_preview", c.length() > 400 ? c.substring(0, 400) : c);
                    record.addProperty("status", "ok");
                } else {
                    record.addProperty("status", "failed");
                    record.addProperty("error", String.valueOf(results.getErrorMessage()));
                }
                decompiled.add(record);
            }
        } finally {
            decompiler.dispose();
        }

        JsonObject report = new JsonObject();
        report.addProperty("status", "ok");
        report.addProperty("ghidra_version", Application.getApplicationVersion());
        report.addProperty("language", currentProgram.getLanguageID().toString());
        report.addProperty("image_base", "0x" + Long.toHexString(
            currentProgram.getImageBase().getOffset()));
        report.addProperty("first_block_start", "0x" + Long.toHexString(
            currentProgram.getMemory().getMinAddress().getOffset()));
        report.addProperty("source_sha256", sha256);
        report.addProperty("source_size", declaredSize);
        report.addProperty("executable_sha256", currentProgram.getExecutableSHA256());
        report.add("blocks", blockReport);
        report.addProperty("functions_requested", starts.size());
        report.addProperty("functions_created", created);
        report.addProperty("functions_disassembled", disassembled);
        report.add("decompile", decompiled);
        Gson gson = new GsonBuilder().setPrettyPrinting().create();
        try (Writer writer = Files.newBufferedWriter(outPath, StandardCharsets.UTF_8)) {
            gson.toJson(report, writer);
            writer.write("\n");
        }
        println("AvcamOrilImport: " + created + " functions, " + blockReport.size()
            + " blocks, sha256=" + sha256);
    }
}

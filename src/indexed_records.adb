with Ada.Unchecked_Deallocation;
with Ada.Directories;
with Zstd_Codec;

package body Indexed_Records is
   use Ada.Streams;
   use type Ada.Streams.Stream_IO.Count;
   use type Ada.Containers.Count_Type;
   package IO renames Ada.Streams.Stream_IO;
   Max_Frame : constant U64 := 16#4000_0000#;
   Max_Frames : constant U64 := 16#0800_0000#;
   Max_U32 : constant U64 := 16#FFFF_FFFF#;
   Index_Magic : constant U64 := 16#184D_2A5D#;
   Seek_Magic : constant U64 := 16#184D_2A5E#;
   End_Magic : constant U64 := 16#8F92_EAB1#;
   Zstd_Magic : constant U64 := 16#FD2F_B528#;
   Tag : constant String := "ARIDX001";
   procedure Free is new Ada.Unchecked_Deallocation (Bytes, Byte_Access);

   procedure Require (OK : Boolean; Why : String) is
   begin
      if not OK then raise Format_Error with Why; end if;
   end Require;

   function Get (B : Bytes; At_Pos : Natural; N : Positive) return U64 is
      Value : U64 := 0;
   begin
      Require (N <= 8 and then At_Pos <= B'Length and then N <= B'Length - At_Pos,
               "truncated integer");
      for J in 0 .. N - 1 loop
         Value := Value or Interfaces.Shift_Left
           (U64 (B (B'First + Stream_Element_Offset (At_Pos + J))), 8 * J);
      end loop;
      return Value;
   end Get;

   procedure Put (B : in out Bytes; At_Pos : Natural; N : Positive; Value : U64) is
   begin
      Require (N <= 8 and then At_Pos <= B'Length and then N <= B'Length - At_Pos,
               "integer output extent");
      for J in 0 .. N - 1 loop
         B (B'First + Stream_Element_Offset (At_Pos + J)) :=
           Stream_Element (Interfaces.Shift_Right (Value, 8 * J) and 255);
      end loop;
   end Put;

   procedure Put_Tag (B : in out Bytes; At_Pos : Natural) is
   begin
      for J in Tag'Range loop
         B (B'First + Stream_Element_Offset (At_Pos + J - 1)) :=
           Stream_Element (Character'Pos (Tag (J)));
      end loop;
   end Put_Tag;

   function Has_Tag (B : Bytes; At_Pos : Natural) return Boolean is
   begin
      if At_Pos > B'Length or else 8 > B'Length - At_Pos then return False; end if;
      for J in Tag'Range loop
         if B (B'First + Stream_Element_Offset (At_Pos + J - 1)) /=
           Stream_Element (Character'Pos (Tag (J))) then return False; end if;
      end loop;
      return True;
   end Has_Tag;

   procedure Reserve (W : in out Writer; Needed : Natural) is
      Capacity : Natural := (if W.Buffer = null then 0 else W.Buffer'Length);
      Replacement : Byte_Access;
   begin
      Require (U64 (Needed) <= Max_Frame, "frame exceeds reference limit");
      if Needed <= Capacity then return; end if;
      Capacity := Natural'Max (Needed, Natural'Max (4096, Capacity * 2));
      Capacity := Natural'Min (Capacity, Natural (Max_Frame));
      Replacement := new Bytes (1 .. Stream_Element_Offset (Capacity));
      if W.Used > 0 then
         Replacement (1 .. Stream_Element_Offset (W.Used)) :=
           W.Buffer (1 .. Stream_Element_Offset (W.Used));
      end if;
      Free (W.Buffer);
      W.Buffer := Replacement;
   end Reserve;

   procedure Flush (W : in out Writer) is
   begin
      Require (U64 (W.Table.Length) + 2 <= Max_Frames, "too many frames");
      declare
         Plain : Bytes renames W.Buffer (1 .. Stream_Element_Offset (W.Used));
         Encoded : constant Bytes := Zstd_Codec.Compress (Plain);
      begin
         Require (U64 (Encoded'Length) <= Max_U32, "compressed frame too large");
         W.Table.Append (Frame_Info'(Offset => U64 (IO.Index (W.File)) - 1,
           Compressed => U64 (Encoded'Length), Decoded => U64 (W.Used),
           Records => W.Group_Records, First => W.Total - W.Group_Records));
         IO.Write (W.File, Encoded);
      end;
      W.Used := 0;
      W.Group_Records := 0;
   end Flush;

   procedure Create
     (W : in out Writer; Path : String; Block_Target : Positive := 262_144;
      Max_Record_Bytes : Positive := 2_097_152; Mode : Group_Mode := Grouped) is
   begin
      Require (not W.Active, "writer already open");
      Require (not Ada.Directories.Exists (Path), "output already exists");
      Require (U64 (Block_Target) <= Max_Frame and then
               U64 (Max_Record_Bytes) + 8 <= Max_Frame, "invalid record or block limit");
      W.Block_Target := Block_Target; W.Max_Record := Max_Record_Bytes;
      W.Mode := Mode; W.Used := 0; W.Group_Records := 0; W.Total := 0;
      W.Table.Clear;
      Reserve (W, 1);
      IO.Create (W.File, IO.Out_File, Path);
      W.Active := True;
   end Create;

   procedure Append (W : in out Writer; Data : Bytes) is
      Serial_Size : constant Natural := Data'Length + 8;
   begin
      Require (W.Active, "writer not open");
      Require (Data'Length <= W.Max_Record, "record exceeds limit");
      if W.Used > 0 and then (W.Mode = Per_Record or else
        (W.Mode = Grouped and then U64 (W.Used) + U64 (Serial_Size) > U64 (W.Block_Target)))
      then Flush (W); end if;
      Require (U64 (W.Used) + U64 (Serial_Size) <= Max_Frame, "frame exceeds reference limit");
      Reserve (W, W.Used + Serial_Size);
      Put (W.Buffer.all, W.Used, 8, U64 (Data'Length));
      W.Buffer (Stream_Element_Offset (W.Used + 9) ..
                Stream_Element_Offset (W.Used + Serial_Size)) := Data;
      W.Used := W.Used + Serial_Size;
      W.Group_Records := W.Group_Records + 1;
      W.Total := W.Total + 1;
   end Append;

   procedure Close (W : in out Writer) is
   begin
      Require (W.Active, "writer not open");
      if W.Used > 0 or else W.Table.Is_Empty then Flush (W); end if;
      declare
         N : constant Natural := Natural (W.Table.Length);
         Plain : Bytes (1 .. Stream_Element_Offset (32 + 8 * N)) := (others => 0);
      begin
         Put_Tag (Plain, 0); Put (Plain, 8, 4, 1);
         Put (Plain, 16, 8, W.Total); Put (Plain, 24, 8, U64 (N));
         for J in 1 .. N loop Put (Plain, 32 + 8 * (J - 1), 8, W.Table (J).Records); end loop;
         declare
            Encoded : constant Bytes := Zstd_Codec.Compress (Plain);
            Outer : Bytes (1 .. 16) := (others => 0);
            Table : Bytes (1 .. Stream_Element_Offset (17 + 8 * (N + 1))) := (others => 0);
            Index_Length : constant U64 := U64 (Encoded'Length) + 16;
         begin
            Require (Index_Length <= Max_U32, "index exceeds size field");
            Put (Outer, 0, 4, Index_Magic); Put (Outer, 4, 4, Index_Length - 8);
            Put_Tag (Outer, 8); IO.Write (W.File, Outer); IO.Write (W.File, Encoded);
            Put (Table, 0, 4, Seek_Magic); Put (Table, 4, 4, U64 (8 * (N + 1) + 9));
            for J in 1 .. N loop
               Put (Table, 8 + 8 * (J - 1), 4, W.Table (J).Compressed);
               Put (Table, 12 + 8 * (J - 1), 4, W.Table (J).Decoded);
            end loop;
            Put (Table, 8 + 8 * N, 4, Index_Length);
            Put (Table, 8 + 8 * (N + 1), 4, U64 (N + 1));
            Put (Table, 13 + 8 * (N + 1), 4, End_Magic);
            IO.Write (W.File, Table);
         end;
      end;
      IO.Close (W.File); W.Active := False; Free (W.Buffer);
   end Close;

   procedure Abort_Write (W : in out Writer) is
   begin
      if IO.Is_Open (W.File) then IO.Close (W.File); end if;
      W.Active := False; Free (W.Buffer);
   end Abort_Write;

   function Read_At (R : in out Reader; Offset, Length : U64) return Bytes is
   begin
      Require (Offset <= R.File_Bytes and then Length <= R.File_Bytes - Offset,
               "read outside archive");
      Require (Length <= U64 (Natural'Last), "read length overflow");
      declare
         Data : Byte_Access := new Bytes (1 .. Stream_Element_Offset (Length));
         Last : Stream_Element_Offset;
      begin
         IO.Set_Index (R.File, IO.Count (Offset + 1));
         IO.Read (R.File, Data.all, Last);
         Require (Last = Data'Last, "short archive read");
         R.Read_Bytes := R.Read_Bytes + Length;
         return Result : Bytes := Data.all do Free (Data); end return;
      exception
         when others => Free (Data); raise;
      end;
   end Read_At;

   procedure Open
     (R : in out Reader; Path : String; Max_Index_Bytes : Positive := 67_108_864;
      Max_Record_Bytes : Positive := 2_097_152) is
      N, Table_Size, Table_Offset, Sum : U64;
   begin
      Require (not R.Active, "reader already open");
      R.Table.Clear; R.Read_Bytes := 0; R.Decoded_Bytes := 0;
      R.Max_Record := Max_Record_Bytes;
      IO.Open (R.File, IO.In_File, Path); R.Active := True;
      R.File_Bytes := U64 (IO.Size (R.File));
      Require (R.File_Bytes >= 33, "archive too short");
      declare Footer : constant Bytes := Read_At (R, R.File_Bytes - 9, 9); begin
         Require (Get (Footer, 5, 4) = End_Magic and then Get (Footer, 4, 1) = 0,
                  "seek footer magic or descriptor");
         N := Get (Footer, 0, 4);
      end;
      Require (N >= 2 and then N <= Max_Frames, "seek frame count");
      Table_Size := 17 + 8 * N;
      Require (Table_Size <= R.File_Bytes and then Table_Size <= U64 (Max_Index_Bytes),
               "seek table exceeds index bound");
      Table_Offset := R.File_Bytes - Table_Size;
      declare Table : constant Bytes := Read_At (R, Table_Offset, Table_Size); begin
         Require (Get (Table, 0, 4) = Seek_Magic and then Get (Table, 4, 4) = Table_Size - 8,
                  "seek table header");
         Sum := 0;
         for J in 1 .. Natural (N) loop
            declare
               C : constant U64 := Get (Table, 8 + 8 * (J - 1), 4);
               D : constant U64 := Get (Table, 12 + 8 * (J - 1), 4);
            begin
               Require (C > 0 and then D <= Max_Frame and then Sum <= Table_Offset
                        and then C <= Table_Offset - Sum, "seek extent or size");
               if J = Natural (N) then
                  Require (D = 0 and then C >= 16 and then C <= U64 (Max_Index_Bytes),
                           "ordinal index entry");
                  R.Metadata_Bytes := Table_Size + C;
               else
                  R.Table.Append (Frame_Info'(Offset => Sum, Compressed => C,
                    Decoded => D, Records => 0, First => 0));
                  declare Header : constant Bytes := Read_At (R, Sum, 5); begin
                     Require (C >= 9 and then Get (Header, 0, 4) = Zstd_Magic and then
                              (Get (Header, 4, 1) and 4) /= 0, "data frame header/checksum");
                  end;
               end if;
               Sum := Sum + C;
            end;
         end loop;
         Require (Sum = Table_Offset, "unexplained archive bytes");
         declare
            C : constant U64 := Get (Table, 8 + 8 * (Natural (N) - 1), 4);
            Outer : constant Bytes := Read_At (R, Sum - C, C);
            Expected : constant U64 := 32 + 8 * (N - 1);
         begin
            Require (Get (Outer, 0, 4) = Index_Magic and then Get (Outer, 4, 4) = C - 8
                     and then Has_Tag (Outer, 8), "ordinal index header");
            Require (Expected <= U64 (Max_Index_Bytes), "decoded index exceeds limit");
            declare
               Plain : constant Bytes := Zstd_Codec.Decompress
                 (Outer (Outer'First + 16 .. Outer'Last), Stream_Element_Count (Expected));
               Cumulative : U64 := 0;
            begin
               R.Decoded_Bytes := R.Decoded_Bytes + Expected;
               Require (Has_Tag (Plain, 0) and then Get (Plain, 8, 4) = 1 and then
                        Get (Plain, 12, 4) = 0 and then Get (Plain, 24, 8) = N - 1,
                        "ordinal index version/flags/count");
               R.Total := Get (Plain, 16, 8);
               for J in 1 .. Natural (N) - 1 loop
                  declare F : Frame_Info := R.Table (J); begin
                     F.Records := Get (Plain, 32 + 8 * (J - 1), 8);
                     Require ((F.Records > 0 and then F.Records <= F.Decoded / 8) or else
                              (N = 2 and then R.Total = 0 and then F.Records = 0 and then F.Decoded = 0),
                              "invalid frame record count");
                     Require (F.Records <= U64'Last - Cumulative, "record count overflow");
                     F.First := Cumulative; Cumulative := Cumulative + F.Records;
                     R.Table.Replace_Element (J, F);
                  end;
               end loop;
               Require (Cumulative = R.Total, "record total mismatch");
            end;
         end;
      end;
   exception
      when others =>
         if IO.Is_Open (R.File) then IO.Close (R.File); end if;
         R.Active := False;
         raise;
   end Open;

   procedure Close (R : in out Reader) is
   begin
      if IO.Is_Open (R.File) then IO.Close (R.File); end if;
      R.Active := False; R.Table.Clear;
   end Close;

   function Decode_Frame (R : in out Reader; J : Positive) return Bytes is
      F : constant Frame_Info := R.Table (J);
      Encoded : constant Bytes := Read_At (R, F.Offset, F.Compressed);
      Plain : constant Bytes := Zstd_Codec.Decompress (Encoded, Stream_Element_Count (F.Decoded));
      Pos : Natural := 0;
      Records : U64 := 0;
   begin
      R.Decoded_Bytes := R.Decoded_Bytes + F.Decoded;
      while Pos < Plain'Length loop
         declare Length : constant U64 := Get (Plain, Pos, 8); begin
            Require (Length <= U64 (R.Max_Record), "record exceeds configured limit");
            Require (Length <= U64 (Plain'Length - Pos - 8), "record crosses frame");
            Pos := Pos + 8 + Natural (Length); Records := Records + 1;
         end;
      end loop;
      Require (Records = F.Records, "decoded record count mismatch");
      return Plain;
   end Decode_Frame;

   function Read (R : in out Reader; Ordinal : U64) return Bytes is
      Low : Positive := 1;
      High : Natural := Natural (R.Table.Length);
      J : Positive;
   begin
      Require (R.Active and then Ordinal < R.Total, "ordinal out of range");
      while Low < High loop
         J := Low + (High - Low) / 2;
         if Ordinal < R.Table (J).First + R.Table (J).Records then High := J;
         else Low := J + 1; end if;
      end loop;
      declare
         Plain : constant Bytes := Decode_Frame (R, Low);
         Pos : Natural := 0;
         Target : constant U64 := Ordinal - R.Table (Low).First;
         I : U64 := 0;
      begin
         while I < Target loop
            Pos := Pos + 8 + Natural (Get (Plain, Pos, 8)); I := I + 1;
         end loop;
         declare Length : constant Natural := Natural (Get (Plain, Pos, 8)); begin
            return Plain (Plain'First + Stream_Element_Offset (Pos + 8) ..
                          Plain'First + Stream_Element_Offset (Pos + 7 + Length));
         end;
      end;
   end Read;

   procedure Validate (R : in out Reader) is
   begin
      Require (R.Active, "reader not open");
      for J in 1 .. Natural (R.Table.Length) loop
         declare Data : constant Bytes := Decode_Frame (R, J); pragma Unreferenced (Data); begin null; end;
      end loop;
   end Validate;

   procedure Unpack (R : in out Reader; Output_Path : String) is
      Output : IO.File_Type;
   begin
      Require (not Ada.Directories.Exists (Output_Path), "output already exists");
      IO.Create (Output, IO.Out_File, Output_Path);
      for J in 1 .. Natural (R.Table.Length) loop
         declare Data : constant Bytes := Decode_Frame (R, J); begin IO.Write (Output, Data); end;
      end loop;
      IO.Close (Output);
   exception
      when others => if IO.Is_Open (Output) then IO.Close (Output); end if; raise;
   end Unpack;

   function Count (R : Reader) return U64 is (R.Total);
   function Frame_Count (R : Reader) return Natural is (Natural (R.Table.Length));
   function Compressed_Size (R : Reader; Frame : Positive) return U64 is (R.Table (Frame).Compressed);
   function Decoded_Size (R : Reader; Frame : Positive) return U64 is (R.Table (Frame).Decoded);
   function Archive_Bytes (R : Reader) return U64 is (R.File_Bytes);
   function Index_Bytes (R : Reader) return U64 is (R.Metadata_Bytes);
   function Bytes_Read (R : Reader) return U64 is (R.Read_Bytes);
   function Bytes_Decoded (R : Reader) return U64 is (R.Decoded_Bytes);
end Indexed_Records;
